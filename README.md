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
