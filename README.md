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
live consumption sensor (which couldn't backfill history). Instead it imports hourly usage as
external [long-term statistics](#long-term-statistics), backfilling ~12 months on first setup
and topping up every 30 minutes, and complements them with [per-meter
sensors](#devices-and-sensors) for at-a-glance state and automations. Every meter on the
account gets its own device and statistics series.

## Installation

Requires [HACS](https://hacs.xyz). The integration is awaiting review for the
HACS default store; until then it installs as a custom repository — one extra
click, same result, including automatic updates.

[![Open your Home Assistant instance and show the add custom repository dialog with this repository pre-filled.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mojo17&repository=ha-enova-power&category=integration)

1. Click the badge above — or, in HACS, open **⋮ → Custom repositories** and add
   `https://github.com/mojo17/ha-enova-power` with type **Integration**.
2. Press **Download** on the Enova Power page that opens, then restart
   Home Assistant.
3. Set up the integration and sign in with your Enova Power My Account
   credentials:

   [![Open your Home Assistant instance and start setting up this integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=enova_power)

   (or **Settings → Devices & Services → Add integration → Enova Power**).

<details>
<summary>Manual installation (without HACS)</summary>

Download the latest [release](https://github.com/mojo17/ha-enova-power/releases/latest)
source archive, copy `custom_components/enova_power/` into your Home Assistant
`config/custom_components/` directory, and restart Home Assistant. You won't
get update notifications this way — HACS is recommended.

</details>

## Configuration

All configuration is via the UI (config flow). Credentials are stored in the config entry;
if your session expires, you'll be prompted to re-authenticate. Your rate plan is detected
automatically from the portal, per meter — the options flow only exists to override a wrong
or missing detection.

## Devices and sensors

Each meter gets a **meter device** (`Enova Power meter <meter id>`) with these sensors:

| Sensor | Unit | What it is |
| --- | --- | --- |
| Latest daily consumption | kWh | Total for the most recent day the portal has published. |
| Latest reading date | timestamp | The day that total belongs to. |
| Total consumption | kWh | Monotonic cumulative usage since first import. The supported source for [utility_meter helpers](#do-i-need-the-utility-meter-helper). |
| Billing cycle consumption | kWh | Usage so far in the current billing cycle. |
| Billing cycle energy cost | CAD | Estimated energy cost so far this cycle (energy line item only). |
| Last bill amount | CAD | The last actual bill; attributes carry the period start/end, days, and kWh. |
| Rate plan | enum | The meter's detected plan: Time-of-Use, Ultra-Low Overnight, or Tiered. |
| Current pricing period | enum | The live period (Off-peak / Mid-peak / On-peak / ULO overnight / Tiered); flips on the hour. |
| Current rate | ¢/kWh | What a kWh costs right now — the active period's rate, or the active tier's on Tiered. |
| Current tier | enum | Tier 1 or Tier 2. Only applies on the Tiered plan; unavailable otherwise. |
| kWh remaining in Tier 1 | kWh | Distance to this cycle's Tier 2 threshold. Tiered plan only. |

One **account device** (`Enova Power`) carries the rate card as diagnostic sensors, one per
published rate in ¢/kWh: TOU off-/mid-/on-peak, ULO overnight/off-/mid-/on-peak, and
Tier 1 / Tier 2. Rates are account-wide and update when Enova changes them (typically
May 1 and November 1).

## Long-term statistics

The energy history lives in external statistics (browse them under **Developer tools →
Statistics**), one set per meter. These are what you chart with statistics cards and what
the Energy dashboard consumes:

| Statistic ID | Unit | Series |
| --- | --- | --- |
| `enova_power:energy_consumption_<meter>` | kWh | Hourly consumption — the Energy dashboard source. |
| `enova_power:energy_tou_off_peak_<meter>`, `…tou_mid_peak…`, `…tou_on_peak…` | kWh | Daily usage classified by the Time-of-Use schedule. |
| `enova_power:energy_ulo_overnight_<meter>`, `…ulo_off_peak…`, `…ulo_mid_peak…`, `…ulo_on_peak…` | kWh | Daily usage classified by the Ultra-Low Overnight schedule. |
| `enova_power:energy_tier1_<meter>`, `…tier2…` | kWh | Daily usage split at the seasonal Tier 1 threshold (600 kWh/cycle in summer, 1000 in winter), accumulated per real billing cycle. |
| `enova_power:cost_tou_off_peak_<meter>`, `…cost_ulo_overnight…`, `…cost_tier1…` (one per bucket above) | CAD | Daily energy cost of each usage bucket at that scheme's current rates — pair with its kWh bucket in the [Energy dashboard](#energy-dashboard). A scheme's bucket costs sum to its `cost_if_*` series. |
| `enova_power:energy_cost_<meter>` | CAD | Daily energy cost under the meter's active plan. |
| `enova_power:cost_if_tou_<meter>`, `…cost_if_ulo…`, `…cost_if_tiered…` | CAD | What the same usage would have cost under each plan — compare them to pick your cheapest plan. |

Semantics worth knowing:

- **Buckets are usage classifications, not plan state.** All three schemes are classified for
  every account regardless of the active plan, so switching plans never orphans a series —
  only `energy_cost` changes which rates it applies. Hours are classified by the meter's
  fixed-EST clock, which matches how the portal itself totals TOU usage.
- **Cost is the energy line item only** — it excludes delivery, regulatory charges, rebates,
  and tax. The actual all-in amount is the *Last bill amount* sensor. The portal only
  publishes *current* rates, so costs apply today's rates and threshold to all history — an
  approximation that self-corrects going forward as rates change.
- Imports are forward-only cumulative sums: re-imports are idempotent, and history survives
  restarts and re-installs.

## Energy dashboard

Under **Settings → Dashboards → Energy → Electricity grid**, add
`Enova Power consumption (<meter id>)` as a consumption source. To see estimated costs
alongside, choose to track total costs and pick `Enova Power energy cost (<meter id>)`.

### Tracking usage by pricing period

To break the dashboard down by period instead, add each bucket as its own consumption
source — e.g. `Enova Power tou off peak (<meter id>)`, `…tou mid peak…`, `…tou on peak…` —
and give each one its matching cost statistic (`Enova Power tou off peak cost (<meter id>)`,
…). The same works for the ULO buckets and for `tier1`/`tier2`.

Use **either** the total consumption source **or** one scheme's buckets, not both at once —
the dashboard sums its sources, so mixing them double-counts your usage. Existing installs
get full history for the cost buckets automatically: the first refresh after upgrading
detects the new series and re-imports history once.

## Do I need the utility meter helper?

Probably not. The [utility_meter](https://www.home-assistant.io/integrations/utility_meter/)
helper accumulates **live** state changes and books each delta at the moment it arrives —
but Enova publishes usage 1–3 days late, so anything utility_meter derives from these
sensors is attributed to the import time, not to when the energy was used. The integration
already provides the correctly-attributed version of everything utility_meter does:

| You want | Use instead of utility_meter |
| --- | --- |
| Daily / weekly / monthly totals | The Energy dashboard, or a Statistics Graph card on `enova_power:energy_consumption_<meter>` with the matching period. |
| Peak / off-peak (tariff) splits | The `tou_*` / `ulo_*` / `tier*` statistics — each hour is classified by when it was actually used. |
| Billing-cycle tracking | The *Billing cycle consumption* / *energy cost* sensors, aligned to your real meter-read cycles (which utility_meter's fixed cron cycles can't follow). |
| Cost tracking | The `energy_cost` and `cost_if_*` statistics. |

If you still want one — e.g. for automations that need a month-to-date total as entity
state — the one supported recipe is:

- **Source:** the **Total consumption** sensor (monotonic, never resets, and immune to
  billing-cycle boundary gaps).
- **Cycle:** monthly or longer. Totals will be right overall, give or take the 1–3 day
  publication lag at cycle boundaries.

What **not** to do:

- **Daily or weekly cycles** — usage lands in the wrong day's bucket, and a catch-up after
  downtime dumps several days into one.
- **Tariffs on the utility_meter** (switching its select from *Current pricing period*) —
  deltas get booked to whatever period is active at import time, not when the energy was
  used. The `tou_*` / `ulo_*` statistics are the correct version of this.
- **Other sensors as the source** — *Latest daily consumption* skips days on multi-day
  catch-ups, and *Billing cycle consumption* silently drops usage published after a bill
  closes.

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
