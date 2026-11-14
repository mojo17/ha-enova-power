"""Daily dependency report: known vulnerabilities and outdated pins/actions.

Run by .github/workflows/dependency-audit.yml with the interpreter of a venv
that has ``pip-audit`` installed (pip-audit brings ``packaging``). Writes a
Markdown report and exposes ``count``/``title`` as step outputs; the workflow
turns that into a single tracking issue.

Three checks:

* **Vulnerabilities** — pip-audit on the runtime requirements twice: as a
  fresh install resolves them today, and at each requirement's minimum
  allowed version (a ``>=`` floor that still admits a vulnerable release is
  worth raising even when the latest is fine).
* **Outdated Python dependencies** — a declared constraint that excludes the
  latest PyPI release (an ``==`` pin behind, or a ``<`` cap). Plain ``>=``
  floors are never "outdated": they already allow the latest.
* **Outdated GitHub Actions** — a ``uses: owner/repo@vN`` whose major is behind
  the action's latest release. Branch refs (``@main``, ``@release/v1``) are
  skipped.

The vendored copy lives in both ha-enova-power and enovapower; keep them in
sync.

Exit status is non-zero only when a check itself breaks (resolution failure,
network error), so a red run means "the audit is broken", and findings are
reported through the issue instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

FINGERPRINT_PREFIX = "dependency-audit fingerprint:"


@dataclass(frozen=True)
class Vuln:
    package: str
    version: str
    vuln_id: str
    aliases: tuple[str, ...]
    fix_versions: tuple[str, ...]
    where: str


@dataclass(frozen=True)
class OutdatedPackage:
    name: str
    declared: str
    latest: str


@dataclass(frozen=True)
class OutdatedAction:
    action: str
    used: str
    latest: str
    workflow: str


def read_requirements(paths: list[Path]) -> list[Requirement]:
    """Parse requirement lines, skipping comments, blanks and pip options."""
    reqs: list[Requirement] = []
    for path in paths:
        for raw in path.read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                reqs.append(Requirement(line))
    return reqs


def run_pip_audit(lines: list[str], *, no_deps: bool) -> dict:
    """Run pip-audit on requirement lines and return its JSON report."""
    with tempfile.TemporaryDirectory() as tmp:
        req_file = Path(tmp, "requirements.txt")
        req_file.write_text("\n".join(lines) + "\n")
        out_file = Path(tmp, "audit.json")
        cmd = [sys.executable, "-m", "pip_audit", "-r", str(req_file),
               "-f", "json", "-o", str(out_file), "--progress-spinner", "off"]
        if no_deps:
            cmd.append("--no-deps")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        # Exit 1 means "vulnerabilities found" *or* "failed"; only a written
        # report distinguishes the two.
        if not out_file.exists():
            sys.stderr.write(proc.stdout + proc.stderr)
            raise SystemExit(f"pip-audit failed (exit {proc.returncode})")
        return json.loads(out_file.read_text())


def minimum_allowed(req: Requirement) -> str | None:
    """Return ``name==floor`` for a ``>=``/``~=`` floor, else None.

    An ``==`` pin has no separate floor: the fresh install already audits it.
    """
    if any(s.operator == "==" for s in req.specifier):
        return None
    floors = [s.version for s in req.specifier if s.operator in (">=", "~=")]
    if not floors:
        return None
    floor = min(floors, key=Version)
    marker = f"; {req.marker}" if req.marker else ""
    return f"{req.name}=={floor}{marker}"


def audit(runtime: list[Requirement]) -> list[Vuln]:
    vulns: set[Vuln] = set()

    def collect(report: dict, where: str) -> None:
        for dep in report["dependencies"]:
            for v in dep.get("vulns", []):
                vulns.add(Vuln(
                    package=dep["name"], version=dep["version"], vuln_id=v["id"],
                    aliases=tuple(v.get("aliases", [])),
                    fix_versions=tuple(v.get("fix_versions", [])), where=where,
                ))

    if not runtime:
        return []
    collect(run_pip_audit([str(r) for r in runtime], no_deps=False), "fresh install")
    floors = [f for f in map(minimum_allowed, runtime) if f]
    if floors:
        collect(run_pip_audit(floors, no_deps=True), "minimum allowed")
    # The same advisory at the same version reported by both passes: keep one.
    unique: dict[tuple[str, str, str], Vuln] = {}
    for v in sorted(vulns, key=lambda v: v.where):
        unique.setdefault((v.package.lower(), v.version, v.vuln_id), v)
    return sorted(unique.values(), key=lambda v: (v.package.lower(), v.version, v.vuln_id))


def http_json(url: str, token: str | None = None) -> dict | None:
    """GET a JSON document; None on 404 (no such package / no releases)."""
    headers = {"Accept": "application/json", "User-Agent": "dependency-audit"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise


def outdated_packages(reqs: list[Requirement]) -> list[OutdatedPackage]:
    seen: set[str] = set()
    found: list[OutdatedPackage] = []
    for req in reqs:
        key = req.name.lower()
        if key in seen or not req.specifier:
            continue
        seen.add(key)
        data = http_json(f"https://pypi.org/pypi/{req.name}/json")
        if data is None:
            continue
        latest = data["info"]["version"]
        if not req.specifier.contains(latest, prereleases=False):
            found.append(OutdatedPackage(req.name, str(req.specifier), latest))
    return found


USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^\s'\"#]+)", re.MULTILINE)
MAJOR_RE = re.compile(r"^v?(\d+)(?:\.\d+)*$")


def outdated_actions(workflows: Path, token: str | None) -> list[OutdatedAction]:
    latest_cache: dict[str, str | None] = {}
    found: dict[OutdatedAction, None] = {}  # ordered set: one row per use site
    for wf in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        for uses in USES_RE.findall(wf.read_text()):
            if uses.startswith(("./", "docker://")) or "@" not in uses:
                continue
            path, ref = uses.split("@", 1)
            used = MAJOR_RE.match(ref)
            if not used:
                continue  # branch or SHA ref: nothing to compare against
            repo = "/".join(path.split("/")[:2])
            if repo not in latest_cache:
                rel = http_json(f"https://api.github.com/repos/{repo}/releases/latest", token)
                latest_cache[repo] = rel["tag_name"] if rel else None
            latest = latest_cache[repo]
            latest_major = MAJOR_RE.match(latest or "")
            if latest_major and int(latest_major[1]) > int(used[1]):
                found[OutdatedAction(path, ref, latest, wf.name)] = None
    return list(found)


def render(vulns: list[Vuln], packages: list[OutdatedPackage],
           actions: list[OutdatedAction]) -> str:
    out: list[str] = []
    if vulns:
        out += ["## Security vulnerabilities", "",
                "| Package | Version | Found in | Upgrade to | Advisories |",
                "|---|---|---|---|---|"]
        groups: dict[tuple[str, str, str], list[Vuln]] = {}
        for v in vulns:
            groups.setdefault((v.package, v.version, v.where), []).append(v)
        for (package, version, where), group in groups.items():
            out.append(f"| {package} | {version} | {where} | {upgrade_target(group)} | "
                       f"{advisory_list(group)} |")
        out += ["", "_fresh install_ = what resolving the declared requirements installs "
                "today; _minimum allowed_ = the lowest version the constraints still "
                "permit (raise the floor). _Upgrade to_ is the lowest release that "
                "fixes every listed advisory.", ""]
    if packages:
        out += ["## Outdated Python dependencies", "",
                "| Package | Declared | Latest |", "|---|---|---|"]
        out += [f"| {p.name} | `{p.declared}` | {p.latest} |" for p in packages]
        out.append("")
    if actions:
        out += ["## Outdated GitHub Actions", "",
                "| Action | Used | Latest | Workflow |", "|---|---|---|---|"]
        out += [f"| {a.action} | `{a.used}` | {a.latest} | `{a.workflow}` |" for a in actions]
        out.append("")
    return "\n".join(out)


def upgrade_target(group: list[Vuln]) -> str:
    """Lowest version that fixes every advisory in the group."""
    unfixed = sum(1 for v in group if not v.fix_versions)
    fixes = [min(map(Version, v.fix_versions)) for v in group if v.fix_versions]
    target = f">= {max(fixes)}" if fixes else ""
    if unfixed:
        target += f"{' (' if target else ''}{unfixed} with no fix yet{')' if target else ''}"
    return target


def advisory_list(group: list[Vuln], shown: int = 5) -> str:
    """Advisory links, preferring the CVE/GHSA alias people recognize."""
    def best_id(v: Vuln) -> str:
        return next((a for a in v.aliases if a.startswith(("CVE-", "GHSA-"))), v.vuln_id)
    links = [advisory_link(best_id(v)) for v in group]
    if len(links) <= shown:
        return ", ".join(links)
    return f"{', '.join(links[:shown])} and {len(links) - shown} more"


def advisory_link(vuln_id: str) -> str:
    if vuln_id.startswith("GHSA-"):
        return f"[{vuln_id}](https://github.com/advisories/{vuln_id})"
    if vuln_id.startswith(("CVE-", "PYSEC-")):
        return f"[{vuln_id}](https://osv.dev/vulnerability/{vuln_id})"
    return vuln_id


def summary_title(vulns: list, packages: list, actions: list) -> str:
    parts = []
    if vulns:
        parts.append(f"{len(vulns)} vulnerabilit{'y' if len(vulns) == 1 else 'ies'}")
    outdated = len(packages) + len(actions)
    if outdated:
        parts.append(f"{outdated} outdated")
    return "Dependency audit: " + ", ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--runtime", type=Path, action="append", default=[],
                    help="requirements shipped to users: audited and version-checked")
    ap.add_argument("--extra", type=Path, action="append", default=[],
                    help="dev/test requirements: version-checked only")
    ap.add_argument("--workflows", type=Path, default=Path(".github/workflows"))
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    runtime = read_requirements(args.runtime)
    extra = read_requirements(args.extra)

    vulns = audit(runtime)
    packages = outdated_packages(runtime + extra)
    actions = outdated_actions(args.workflows, token)

    body = render(vulns, packages, actions)
    count = len(vulns) + len(packages) + len(actions)
    fingerprint = hashlib.sha256(body.encode()).hexdigest()[:16]
    args.out.write_text(f"<!-- {FINGERPRINT_PREFIX} {fingerprint} -->\n{body}")
    title = summary_title(vulns, packages, actions) if count else "Dependency audit: clean"
    print(f"{title}\n\n{body}" if count else title)

    if gh_output := os.environ.get("GITHUB_OUTPUT"):
        with open(gh_output, "a") as fh:
            fh.write(f"count={count}\nfingerprint={fingerprint}\ntitle={title}\n")


if __name__ == "__main__":
    main()
