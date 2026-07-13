# Power Monitor

A local macOS SolarEdge power monitor backed by SQLite. It collects 15-minute
SolarEdge readings, applies Synergy EV Add On tariff buckets, and generates a
console report plus a clean HTML dashboard.

The core collector and dashboard use Python's standard library. Collection also
requires the macOS `curl` command. Automated SolarEdge session refresh is
optional and requires Playwright.

## Role

PowerMonitor is not a Home Assistant replacement. Home Assistant can remain the
home automation system and can feed PowerMonitor data, such as OCPP charger
session snapshots. PowerMonitor's job is narrower: normalize energy and EV
charging data, explain costs, compare tariff plans, run What If scenarios, and
turn those calculations into decisions.

Source-specific code lives in connectors/importers such as SolarEdge
collection, Home Assistant OCPP import, and Ocular Excel import. The report,
scenario, and recommendation engines are source-agnostic and work from the
internal models documented in `docs/data-model.md`.

PowerMonitor now uses a Universal Measurement Framework as the provider-neutral
layer below analytics. Providers expose standardized measurements such as solar
generation, household consumption, grid import/export, future battery
charge/discharge, and future tariff data. Each measurement carries provider,
timestamp, and data-quality metadata. SolarEdge is the first implementation and
maps the existing normalized `readings` data into this framework so existing
reports and analytics continue to work while future providers can be added
behind the same abstraction.

Provider IDs are unique, and each provider advertises capabilities through the
registry before analytics request measurements. This keeps future inverter,
meter, MQTT, and Home Assistant integrations contained within their provider
implementation instead of leaking vendor-specific branches into reports or
recommendations.

For the broader product direction, see `VISION.md`. For the current closed
beta readiness assessment, see `docs/beta-readiness-audit.md`.

## Home Profile

PowerMonitor has a local Home Profile at `config/home_profile.json`. The Home
Profile describes the household independently of measurement providers: region,
retailer, current tariff, solar, battery, EV, charger, household monitoring, and
Home Assistant availability.

This profile is the foundation for setup guidance, capability detection,
simulations, recommendations, and future Home Intelligence. It is separate from
provider credentials and measurements, and it should not contain cookies,
tokens, account numbers, NMI values, or raw energy readings. See
`docs/home-profile.md` for the model and persistence boundary.

Create or update the profile with the local CLI wizard:

```sh
python3 collector/setup_home_profile.py
```

The wizard stores declared home facts only. It does not configure live
providers or change dashboard behaviour.

## Demo Mode

PowerMonitor can run with deterministic fictional demo data when no live
providers are configured. Demo mode creates a realistic WA household with a
Home Profile, representative Synergy bills, 15-minute energy readings, EV
charging sessions, recommendations, timeline events, collector health, and
fictional Perth weather.

Generate the demo dashboard:

```sh
python3 collector/report.py --demo
open reports/demo/index.html
```

Run the local dashboard server with demo data:

```sh
python3 collector/web.py --demo
open http://localhost:8765
```

You can also enable it with `POWMONITOR_DEMO=1`. Demo files are kept separate
from production data under `data/demo/`, `config/demo/`, `logs/demo/`, and
`reports/demo/`. The seeded household is fictional and should not be replaced
with real customer, account, NMI, cookie, token, or credential data.

## Setup

1. Use Python 3.9 or newer. PowerMonitor is tested for Python 3.14
   compatibility.
2. Create and use the project virtual environment so manual runs and
   LaunchAgents use the same Python packages:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install --upgrade pip
   ```

3. Install the local runtime packages used by optional integrations:

   ```sh
   .venv/bin/python -m pip install playwright pypdf
   .venv/bin/python -m playwright install chromium
   ```

   `pdfminer.six` is also supported as a pure-Python fallback. The importer can
   use the system `pdftotext` command when it is already installed, but
   Homebrew/poppler is not required.
4. Verify the active Python path before troubleshooting runtime issues:

   ```sh
   .venv/bin/python -c 'import sys; print(sys.executable)'
   ```

   The System page also shows the active Python executable and dependency
   availability under Advanced Diagnostics.
5. Configure SolarEdge with username/email and password through Provider
   Management, or use the manual cookie fallback described below.
6. Keep cookies and credentials private. They are ignored by `.gitignore` and must not be
   printed or committed.
7. Ensure the SQLite database is at `data/power.sqlite`.

Scripts resolve the project directory from their own location, so they work
from any current directory.

## Run

Collect and import recent SolarEdge data:

```sh
python3 collector/collect_today.py
```

`collector/collect_today.py` keeps its filename for LaunchAgent compatibility,
but it now behaves as a recent-data collector. On each run it fetches each day
from `today - (collection_backfill_days - 1)` through today, one day at a
time. The value is clamped from 1 to 7 days to stay within SolarEdge's recent
quarter-hour Power-data window.

This backfill means that if the SolarEdge session expires, automatic
credential recovery or a manual cookie refresh within SolarEdge's available
Power-data window can recover recent gaps. Each day is validated and imported
independently with `INSERT OR REPLACE`, so repeat runs refresh existing
timestamps without creating duplicate readings.

After at least one successful day import, the collector automatically runs
`collector/report.py` once so the static file served by the local dashboard
stays current. Collection and report-generation outcomes are logged
separately. If collection succeeds but report generation fails, collector
health keeps the successful collection timestamp while showing a
report-generation warning.

Generate the console and HTML reports:

```sh
python3 collector/report.py
open reports/index.html
```

Run those two commands whenever you want to regenerate and open the latest
local dashboard. The HTML is self-contained, works offline, and includes print
styles for saving a clean PDF from the browser.

The dashboard is split into local hash-routed pages with no external assets:
`#home`, `#glance`, `#recommendations`, `#bills`, `#solar`, `#ev`, `#whatif`,
`#timeline`, `#analytics`, and `#system`.
The System page shows collector health, EV source counts grouped by
`ev_sessions.source`, and a privacy-safe configuration summary. Print/PDF mode
expands all pages so the complete report can still be saved.

Run the local dashboard server:

```sh
python3 collector/web.py
open http://localhost:8765
```

By default the server binds to `0.0.0.0:8765`, so other devices on the same
trusted local network can open the dashboard using the Mac's LAN address. The
dashboard is still available locally at `http://localhost:8765`. Override the
host or port without editing source by setting `POWMONITOR_HOST` and
`POWMONITOR_PORT`, or by adding `web_server.host` and `web_server.port` to
`config/settings.json`. For localhost-only development, set
`POWMONITOR_HOST=127.0.0.1` or `web_server.host` to `127.0.0.1` explicitly. It
serves the generated printable dashboard at `/`, a kiosk-friendly Glance Mode
at `/glance`, live summary JSON at `/api/summary`, and health information at
`/api/health`. The dashboard reloads every five minutes and remains fully
offline with no CDN or internet assets.

The live dashboard period selector supports Today, Yesterday, Last 7 days,
This month, Last month, Year to date, Last calendar year, Current billing
period, Previous billing period, and custom dates. The API also supports week
periods:

```sh
curl 'http://127.0.0.1:8765/api/summary?period=today'
curl 'http://127.0.0.1:8765/api/summary?period=current_billing_period'
curl 'http://127.0.0.1:8765/api/summary?period=custom&start=2026-06-06&end=2026-06-12'
curl 'http://127.0.0.1:8765/api/compare?period_a=current_billing_period&period_b=previous_billing_period'
curl 'http://127.0.0.1:8765/api/compare?start_a=2026-06-06&end_a=2026-06-12&start_b=2026-05-30&end_b=2026-06-05'
```

Supported period names are `today`, `yesterday`, `last_7_days`, `this_week`,
`last_week`, `this_month`, `last_month`, `year_to_date`,
`last_calendar_year`, `current_billing_period`, `previous_billing_period`, and
`custom`. Period boundaries use local Australia/Perth calendar dates.

The Setup page includes a Billing Cycle section for editing the billing cycle
start day, optional retailer label, and supported tariff plan. Saving that
local form updates `config/settings.json` and immediately completes the
Billing Cycle setup task.

The Imports page supports local uploads for:

- Ocular EV charging history Excel exports.
- Synergy PDF bills for actual bill reconciliation.

Synergy bill imports preview extracted invoice details, billing periods, tariff
line items, export credits, GST, and bill totals before anything is committed.
PowerMonitor masks account and NMI identifiers in normal UI/API output and uses
imported bills to compare projected bills with actual retailer bills.
PDF text extraction uses `pdftotext` when available, otherwise it falls back to
the pure-Python `pypdf` or `pdfminer.six` packages installed in the project
environment.

Runtime logs are written to:

- `logs/collect.log`
- `logs/report.log`
- `logs/web.log`

Collector health state is written to `data/collector_health.json`. It records
successful and failed attempts, authentication failures, consecutive failures,
the time since the last successful collection, and the last successful report
generation. The dashboard and `/api/health` display this state plus live report
age in minutes.

## Refreshing SolarEdge Session Cookie

SolarEdge browser sessions expire periodically. When SolarEdge returns HTTP
401 or 403, PowerMonitor records a clear collector status such as `Cookie
expired`, `Credentials missing`, `Sign-in failed`, or `SolarEdge unavailable`
and shows recovery guidance on the dashboard. The collector first tries to
refresh the session automatically with saved SolarEdge username/email and
password credentials, then retries the failed collection once. If automated
refresh is not configured or fails, the existing manual cookie flow remains the
advanced fallback.

### Automated Refresh

Install Playwright for the same Python used by LaunchAgent:

```sh
.venv/bin/python -m pip install playwright
.venv/bin/python -m playwright install chromium
```

Provide SolarEdge credentials through Provider Management, or via environment
variables:

```sh
export SOLAREDGE_USERNAME='you@example.com'
export SOLAREDGE_PASSWORD='your-password'
```

Alternatively, create ignored `config/secrets.json`:

```json
{
  "solaredge_username": "you@example.com",
  "solaredge_password": "your-password"
}
```

The refresh script opens SolarEdge login in headless Chromium, signs in, waits
for a dashboard Power-data request to return HTTP 200, and writes only the
required SolarEdge cookies to `collector/solaredge_cookie.txt`. It never prints
or logs usernames, passwords, credentials, cookie values, tokens, or
authorization headers.

You can test the refresh path manually:

```sh
python3 collector/refresh_solaredge_session.py
python3 collector/test_cookie.py
```

### Manual Fallback

Use this only when automatic SolarEdge sign-in is unavailable.

1. Sign in to the SolarEdge monitoring website in your browser.
2. Open the browser developer tools and inspect a successful SolarEdge
   dashboard data request.
3. Copy that request's complete `Cookie` header value into
   `collector/solaredge_cookie.txt`, replacing the expired value.
4. Test the refreshed cookie with this command.

```sh
python3 collector/test_cookie.py
```

The tester prints only `ok`, `expired/auth failure`, or `network failure`. It
never prints or logs the cookie value.

5. Once the test prints `ok`, collect and regenerate the dashboard:

```sh
python3 collector/collect_today.py
```

Keep `collector/solaredge_cookie.txt` private and out of version control.

Synergy billing periods follow meter-read and charge-period dates, so they are
not necessarily calendar months. Set `current_billing_period_start` to the day
after the previous bill ended. The report uses that explicit date through
today for the hero, Current billing period section, costs, averages, and trends.

`billing_period_start_day` is a fallback only when
`current_billing_period_start` is missing or empty. With no explicit date, a
one-off fallback override can be supplied as a day from 1 to 28:

```sh
POWERMONITOR_BILLING_START_DAY=15 python3 collector/report.py
```

The SolarEdge site ID defaults to the prototype's existing site. It can be
overridden without editing the script:

```sh
POWERMONITOR_SOLAREDGE_SITE_ID=1234567 python3 collector/collect_today.py
```

## Report Calculations

- Import cost is recalculated from each interval's tariff bucket using the
  configured Synergy EV import rates.
- Supply charge is the configured daily charge multiplied by elapsed calendar
  days in the reported period.
- Export credit is exported solar multiplied by the configured export rate.
- Net estimated cost is import cost plus supply charge minus export credit.
- Solar self-consumption is solar used directly divided by solar generated.
- Solar export percentage is exported solar divided by solar generated.
- Estimated overnight EV charging is overnight grid import above the configured
  household baseline, calculated for every 15-minute interval. EV cost uses the
  configured overnight rate and remains a rough estimate.
- Energy averages use days with readings. Average daily net cost uses every
  elapsed billing-period day because supply charges apply every day.

## Settings

Report assumptions live in `config/settings.json`:

```json
{
  "supply_charge_per_day": 1.292269,
  "import_rates": {
    "super_off_peak": 0.086151,
    "overnight": 0.193841,
    "off_peak": 0.236916,
    "peak": 0.538446
  },
  "export_credit_rate": 0.02,
  "collection_backfill_days": 7,
  "ev_baseline_watts": 250,
  "ev_data_source": "home_assistant_ocpp",
  "home_assistant_url": "http://192.168.4.96:8123",
  "billing_period_start_day": 1,
  "current_billing_period_start": "2026-06-06",
  "current_billing_period_end": null,
  "previous_billing_period_start": "2026-04-03",
  "previous_billing_period_end": "2026-06-05",
  "current_plan": "synergy_ev_add_on",
  "comparison_plan": "previous_flat_or_home_plan",
  "comparison_plan_rates": {
    "flat_import_rate": 0.30,
    "supply_charge_per_day": 1.292269,
    "export_credit_rate": 0.02
  },
  "petrol_price_per_litre": 1.80,
  "previous_car_l_per_100km": 8.0,
  "ev_kwh_per_100km": null,
  "ev_odometer_km": null,
  "car_comparison_enabled": false,
  "recommendation_thresholds": {
    "peak_cost_share_pct": 25,
    "peak_shift_kwh_per_day": 5,
    "solar_self_consumption_pct": 60,
    "ev_detected_min_kwh": 0.5,
    "baseload_excess_pct": 20,
    "high_cost_threshold": 300,
    "positive_peak_share_pct": 20,
    "positive_self_consumption_pct": 70
  },
  "failure_email_notification": {
    "enabled": false,
    "failure_after_hours": 24,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_username": "",
    "smtp_password_env": "POWERMONITOR_SMTP_PASSWORD",
    "from_address": "",
    "to_address": "",
    "use_starttls": true
  }
}
```

All monetary rates are dollars per kWh except `supply_charge_per_day`, which is
dollars per day. Update these assumptions when your tariff changes, then run
`python3 collector/report.py` to regenerate the console and HTML analytics.
If the settings file is missing, the report uses the same built-in defaults.
The settings file must contain assumptions only, never cookies or tokens.

`collection_backfill_days` controls how many recent days the collector fetches
on each run. Values below 1 are treated as 1; values above 7 are treated as 7.
The collector fetches days individually and records partial failures in
collector health when at least one day still imports successfully.

Failure email notifications are optional and disabled by default. When enabled,
the collector sends one notification only after a continuing failure exceeds
`failure_after_hours`; it does not send daily reports or success emails. Store
the SMTP password in the environment variable named by `smtp_password_env`,
never in `settings.json`. A successful collection resets notification
suppression for the next separate failure incident.

The previous-period dates document the last known Synergy charge period. They
drive previous-billing-period comparisons when configured.

`current_billing_period_end` is optional. When set, projected-cost
recommendations project through that date. When it is missing or `null`, the
report estimates the current period end using the previous configured billing
period length.

The plan comparison compares measured usage under the configured current EV
Add On rates with `comparison_plan_rates`. It reports current-plan grid import,
supply charge, export credit, net bill, EV charging cost, household usage cost,
and the equivalent Flat/Home plan result. It answers whether the same usage
would have cost more or less on the comparison plan; it is a tariff model
rather than actual bill reconciliation.

The What If scenario engine models billing-period alternatives, including EV
charging shifted to the 9am-3pm EV window, overnight charging, Peak charging,
no EV charging, exported solar self-consumed, and a 10 kWh battery shifting
Peak imports to solar. The API exposes these as `plan_comparison`,
`scenario_analysis`, and `billing_scenarios`.

Car fuel comparison is disabled by default. It only becomes available when
`car_comparison_enabled` is true and distance plus EV efficiency data are
provided. Without distance or charger-session data, the dashboard states:
"EV fuel savings require distance or charger session data."

## Historical Comparisons

PowerMonitor can only compare periods where readings exist. A comparison with
an older period containing no readings will correctly show zero measured
energy, which is not evidence of savings.

For before-EV-Add-On comparisons, import old SolarEdge readings or historical
bill data when available. The database includes a future-compatible
`billing_periods` table:

```text
name
start_date
end_date
actual_bill_amount
plan_name
notes
```

This table is currently a placeholder for future bill reconciliation and does
not alter calculated summaries.

## EV Charging Accuracy

`ev_data_source` supports `estimated`, `ocular_csv`, `ocpp`,
`home_assistant_ocpp`, and `ocular_export`. With `ocular_export`, PowerMonitor
uses actual charger session energy imported from Ocular Excel charging-history
exports. If no actual session data exists for a report period, it falls back to
the overnight estimate and labels it as estimated. Ocular export sessions are
preferred over Home Assistant OCPP snapshots when both exist for a period.

With `estimated`, PowerMonitor counts overnight grid import above
`ev_baseline_watts`. This estimate only covers overnight charging above the
configured baseload. Daytime EV charging is not included unless charger
session data is connected. In particular, EV charging from solar or cheap
9am-3pm super off-peak import is not detected by the estimate.

The database includes an empty future-compatible `ev_sessions` table:

```text
transaction_id
start_time
end_time
last_seen
energy_kwh
first_seen_energy_kwh
last_energy_kwh
duration_minutes
max_power_kw
status
source
cost_estimate
tariff_breakdown_json
cost_partial
start_source
stop_source
notes_json
super_off_peak_kwh
super_off_peak_cost
overnight_kwh
overnight_cost
off_peak_kwh
off_peak_cost
peak_kwh
peak_cost
```

### Home Assistant OCPP Import

Create a Home Assistant long-lived access token:

1. Open Home Assistant at `http://192.168.4.96:8123`.
2. Click your user profile.
3. Under **Long-lived access tokens**, create a token for PowerMonitor.
4. Store it in the environment as `HOME_ASSISTANT_TOKEN` or `HA_TOKEN`.

Alternatively, store it in ignored `config/secrets.json`:

```json
{
  "home_assistant_token": "<token>"
}
```

Do not put the Home Assistant token in `config/settings.json`.

Import the current OCPP charger snapshot:

```sh
python3 collector/import_ha_ocpp.py
python3 collector/report.py
```

The importer polls these Home Assistant entities:

- `sensor.charger_transaction_id`
- `sensor.charger_energy_session`
- `sensor.charger_power_active_import`
- `sensor.charger_current_import`
- `sensor.charger_voltage`
- `sensor.charger_status_connector`
- `sensor.charger_time_session`

Snapshots are stored in `ev_sessions` using `transaction_id`. A changed
transaction ID creates a new session; repeated imports update the same session
with the latest energy, status, `last_seen`, maximum observed power, and an
observed tariff-aware cost estimate.

OCPP cost is estimated from observed polling intervals. On each poll,
PowerMonitor compares the current Home Assistant session energy with the
previous observed value, allocates the positive delta to the Synergy EV tariff
bucket for the poll timestamp, and adds the corresponding cost. If PowerMonitor
starts mid-session, the already-accumulated energy becomes the baseline and is
marked as a partial session cost; earlier energy is not back-costed. If the
session energy decreases, PowerMonitor treats it as a reset/new baseline and
does not create negative cost.

Best accuracy comes from polling during charging every 1-5 minutes, for
example with LaunchAgent or a Home Assistant automation that runs:

```sh
python3 collector/import_ha_ocpp.py
```

A future Ocular CSV importer can write charger sessions with source
`ocular_csv`, while other OCPP integrations can write sessions with source
`ocpp`.

### Import Ocular Charging History

The Ocular app can export LTE Plus V3 charging history as an Excel workbook.
Export all available history each time, then import it:

```sh
python3 collector/import_ocular_export.py /path/to/export.xlsx
python3 collector/report.py
```

The importer reads these columns:

- `Record number`
- `Charger number`
- `Charging amount`
- `Charging start time`
- `Charging stop time`
- `Charging duration`
- `Starting user`
- `Stopping user`

`Record number` is stored as the external session ID in `transaction_id`, with
`source = ocular_export`. Re-importing the same full history is idempotent:
existing records are updated and new records are inserted, so duplicate EV
sessions are not created.

Ocular session cost is calculated by splitting the session duration across the
configured Synergy EV tariff windows and allocating the recorded kWh
proportionally across those windows. The dashboard labels this as
`Calculated session cost`. Home Assistant OCPP costs are labelled
`Calculated charging cost`. The dashboard treats Ocular exports as the stable
actual EV source and shows the tariff breakdown in the Actual EV charging card.

PowerMonitor also stores explicit per-tariff kWh and cost columns for Ocular
sessions, alongside `tariff_breakdown_json`. The importer validates that each
session's tariff kWh total matches the recorded charging amount within
0.01 kWh and that tariff costs sum to the stored cost within $0.01. These
stored fields drive the EV Charging analytics section:

- total EV kWh and cost for the selected period
- average session cost, session size, and effective cost per kWh
- tariff usage by kWh and percentage
- cheapest, most expensive, longest, and highest-power sessions
- charging days, session count, average start time, and average finish time
- recommendations for shifting any Peak charging into cheaper tariff windows
- session history with date, start, finish, duration, kWh, cost, dominant
  tariff, and cost per kWh

## Recommendations

The Recommendation Engine lives in `engine/recommendation_engine.py`. It
evaluates current billing-period, solar, EV, tariff, baseload, trend, and data
quality observations and returns ranked structured recommendations. The
Decision Engine adapts those recommendations for the existing dashboard and
`/api/summary` contracts.

Recommendation thresholds are configured under `recommendation_thresholds` in
`config/settings.json`. The projected-cost alert uses the previous configured
Synergy billing period to infer the expected current-period length when an
explicit current end date is unavailable. Projections are labelled low
confidence below 10 reading days, medium confidence from 10 to 20 days, and
high confidence from 21 days. Low-confidence projections remain amber unless
the actual cost so far already exceeds the configured threshold. Baseload
monitoring uses the median overnight grid demand so detected EV charging does
not dominate the baseload estimate.

## LaunchAgent Automation

Generate LaunchAgents from the current checkout so scheduled jobs use the
project Python instead of `/usr/bin/python3`:

```sh
.venv/bin/python collector/install_launch_agents.py
```

The helper writes plists for:

- `com.local.powermonitor.collector`
- `com.local.powermonitor.web`
- `com.local.powermonitor.ocpp` when the Home Assistant OCPP importer is
  present

These are the only supported LaunchAgent labels. Older installs may have used
an obsolete namespace; the installer unloads and removes those plists
automatically before writing the supported `com.local` jobs.

It prefers `<project>/.venv/bin/python` and prints the exact reload commands.
Reload manually with:

```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.local.powermonitor.collector.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.powermonitor.collector.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.local.powermonitor.web.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.powermonitor.web.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.local.powermonitor.ocpp.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.powermonitor.ocpp.plist
```

Restart a loaded job immediately with:

```sh
launchctl kickstart -k gui/$(id -u)/com.local.powermonitor.collector
launchctl kickstart -k gui/$(id -u)/com.local.powermonitor.web
launchctl kickstart -k gui/$(id -u)/com.local.powermonitor.ocpp
```

Check the interpreter used by a generated plist:

```sh
plutil -p ~/Library/LaunchAgents/com.local.powermonitor.collector.plist | grep ProgramArguments -A3
```

Validate the installed jobs and loaded services:

```sh
python3 collector/validate_launch_agents.py
python3 collector/diagnostics.py
launchctl list | grep powermonitor
```

The validator reports duplicate LaunchAgent plists, duplicate loaded services,
obsolete namespace services, mismatched plist labels or scripts, incorrect
working directories, and missing collector/web/OCPP jobs.

`collector/diagnostics.py` adds a broader runtime audit: canonical report path,
live-dashboard/report hash matching, loaded LaunchAgents, duplicate
PowerMonitor processes, duplicate listeners on port `8765`, report staleness,
LaunchAgent working directory/executable drift, and missing config, database, or
report files. The Settings page also shows privacy-safe runtime diagnostics such
as the Git commit, active Python, working directory, report path, web bind
address, database/config paths, active tariff version, and current health
summary.

The collector safely refreshes existing readings by timestamp. The web server
binds to `0.0.0.0` by default unless `POWMONITOR_HOST` or `web_server.host`
explicitly requests localhost-only development mode.
