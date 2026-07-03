# Enova Power for Home Assistant

[![hacs](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)

> **⚠️ Unofficial project — not affiliated with, endorsed by, or supported by Enova Power Corp.** "Enova Power" is a trademark of its respective owner. Use at your own risk under the Apache-2.0 license.

A Home Assistant integration that reports household electricity consumption from the
[Enova Power](https://enovapower.com) My Account portal (Kitchener-Waterloo, Ontario). It is
a thin wrapper over the [`enovapower`](https://github.com/mojo17/enovapower) library and
feeds your hourly usage into Home Assistant **long-term statistics**, so it appears natively
in the **Energy dashboard** with full history.

## How it works

Enova publishes smart-meter data a few days in arrears, so this integration does **not** use a
live sensor (which couldn't backfill history). Instead it imports hourly usage as external
statistics (`enova_power:energy_consumption`), backfilling ~12 months on first setup and
topping up every 30 minutes. Two informational sensors expose the latest daily total and
reading date.

## Installation (HACS)

1. HACS → Integrations → ⋮ → **Custom repositories** → add `https://github.com/mojo17/ha-enova-power` (category: *Integration*).
2. Install **Enova Power**, then restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Enova Power**, and sign in with your
   Enova Power My Account credentials.

## Configuration

All configuration is via the UI (config flow). Credentials are stored in the config entry;
if your session expires, you'll be prompted to re-authenticate.

## Energy dashboard

Add the **Enova Power consumption** statistic as a consumption source under
**Settings → Dashboards → Energy → Electricity grid**.

## Notes & limitations

- Data lags the current day by ~1–3 days (utility-side), so recent hours fill in later.
- Interval data is in fixed Eastern Standard Time (UTC-5), handled by the library.
- License: Apache-2.0.

## Development

Requires Python 3.14+ (Home Assistant 2026.7+ pins `>=3.14.2`). The helper
scripts create one virtualenv (`.venv`, gitignored) that both runs the tests and
serves a live dev instance — using [`uv`](https://docs.astral.sh/uv/) when
present (it can even fetch Python 3.14 itself), else falling back to `venv`+`pip`:

```bash
./scripts/setup      # create .venv and install test + runtime deps
./scripts/develop    # run live HA on http://localhost:8123 with this component
./scripts/test       # run the pytest suite (mirrors CI); extra args pass through
```

`scripts/develop` symlinks the integration into a throwaway `config/` dir, so
code edits take effect on the next HA restart. Only `config/configuration.yaml`
is tracked — everything HA generates there is ignored.

Prefer to do it by hand:

```bash
pip install -r requirements_test.txt
pytest
```
