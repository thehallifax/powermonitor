# PowerMonitor Data Model

PowerMonitor separates source-specific collection from source-agnostic
analytics.

- **Connectors/importers** are source-specific. They know about SolarEdge,
  Home Assistant OCPP, Ocular exports, cookies, tokens, CSV/XLSX formats, and
  API quirks.
- **Reports, recommendations, scenarios, and plan comparisons** are
  source-agnostic. They should consume the normalized models below and avoid
  caring whether a value came from SolarEdge, Ocular, Home Assistant, or a
  future source.

## Universal Measurement Framework

The Universal Measurement Framework is the provider-neutral foundation between
connectors/importers and analytics. A `MeasurementProvider` exposes
`UniversalMeasurement` records instead of vendor-specific payloads. A
`MeasurementProviderRegistry` can load one or more providers and report which
measurement capabilities are available so downstream features can adapt without
hardcoded assumptions.

Current implementation:

- SolarEdge is the first measurement provider.
- SolarEdge measurements are mapped from the existing normalized `readings`
  table, preserving current import/report behaviour.
- Fronius is available as an opt-in local Solar API measurement provider, but
  is not registered in `DataEngine` by default yet.
- GoodWe is available as an opt-in local inverter measurement provider, but is
  not registered in `DataEngine` by default yet.
- Sungrow is available as an opt-in local inverter measurement provider, but
  is not registered in `DataEngine` by default yet.
- Enphase is available as an opt-in local Envoy/IQ Gateway measurement
  provider, but is not registered in `DataEngine` by default yet.
- `DataEngine` owns registry creation and measurement retrieval.
- Report generation consumes universal measurements for the main period
  summary aggregation.
- Provider IDs are unique. Duplicate provider registration fails fast so
  analytics can safely identify source provenance.
- The minimal provider contract is `provider_id`, `provider_name`,
  `capabilities()`, and `measurements(start, end)`.

Universal measurement types:

- `solar_generation`
- `household_consumption`
- `grid_import`
- `grid_export`
- `battery_charge`
- `battery_discharge`
- `tariff_data`

Every measurement carries metadata:

- `provider_id`
- `source`
- `timestamp`
- `quality`: `measured`, `calculated`, `estimated`, or `unknown`
- optional source record/import identifiers

Provider capability reporting uses the same measurement type names plus unit,
quality, availability, and a note where useful. `MeasurementProviderRegistry`
can report supported and missing measurement types directly; downstream engines
should use those helpers instead of inspecting provider-specific internals.
Future providers such as Fronius, GoodWe, Sungrow, Enphase, Shelly EM, MQTT,
Home Assistant, and Modbus should implement their own measurement provider and
register it with `MeasurementProviderRegistry`; analytics should not need
provider-specific branches for those integrations.

## Home Profile

The Home Profile is the provider-independent description of the household. It is
stored locally in `config/home_profile.json` and captures user-declared facts
such as state/region, retailer, tariff, solar, battery, EV, charger, household
monitoring, and Home Assistant availability.

The Home Profile is not a measurement source. It should be used as durable
household context for setup, capability detection, simulations,
recommendations, and future Home Intelligence. It must not contain credentials,
cookies, tokens, account numbers, NMI values, or raw measurements.

See `docs/home-profile.md` for persistence details and compatibility rules.

## Household Timeline

The Household Timeline is the provider-neutral history of household conditions.
It is stored locally in `config/household_timeline.json` and supports timeline
events such as `tariff_period`, `solar_installation`, `battery_installation`,
`ev_added`, `ev_removed`, `ev_charger_installed`, `major_appliance_change`,
`occupancy_change`, and `custom_note`.

Each event records an id, type, title, effective start/end dates, source,
confidence, optional notes, creation/update timestamps, active state, metadata,
and checksum/version information where useful. Changes written through Settings
append an audit record with previous values, new values, affected event IDs,
timestamp, source, action, and optional reason.

Only `tariff_period` affects calculations today. A tariff period stores a stable
tariff snapshot or catalogue-derived `TariffVersion` reference with provider,
tariff name, version, effective dates, supply charge, import rates, export
credit, tariff windows, source, and notes. Historical measurements are not
stamped with tariff prices; reports resolve the tariff dynamically on each run.

Tariff resolution precedence is deterministic:

1. Explicit household tariff timeline period.
2. Approved historical `TariffVersion` effective for the timestamp.
3. Current configured tariff fallback.
4. Manual fallback with warning.

Overlapping tariff periods are rejected, adjacent periods are allowed, gaps are
reported, and open-ended current periods must be ended before another tariff can
start. When a period spans multiple tariff periods, interval import/export costs
and local-day supply charges use the active tariff for each timestamp. Battery
replay receives the resolved interval rate, so flat-rate history does not gain
cheap-grid arbitrage from a later EV time-of-use plan.

## Home Capabilities

Home capability detection combines the provider-independent Home Profile with
measurement capabilities from `MeasurementProviderRegistry`. It reports whether
PowerMonitor can currently support backend features such as billing analysis,
solar generation analysis, household consumption analysis, grid import/export
analysis, EV charging analysis, battery analysis, tariff optimisation, energy
timeline readiness, and future Home Intelligence readiness.

Each detected capability distinguishes `enabled` from `available`: enabled
means the feature is relevant to the declared household, while available means
PowerMonitor has enough declared facts and/or provider measurements to support
it now. Capability records also carry unavailable reasons, supporting
profile/provider sources, confidence, and data quality.

## Battery Product Catalogue

The Battery Product Catalogue is source-controlled decision-support data stored
under `data/catalogues/batteries/`. It maps abstract battery sizing outputs
such as `15 kWh usable` to real product configurations that are reasonably
close to the modelled usable capacity.

The catalogue is not runtime state and is not a connector. It must not contain
credentials, customer identifiers, account numbers, quotes, cookies, tokens, or
raw measurements. Each product record carries a stable `product_id`, source
metadata, data-confidence label, capacity and module fields, optional power and
warranty fields, compatibility notes, optional indicative cost ranges, lifecycle
status, version, and checksum.

Matching rules are intentionally conservative:

1. Usable-capacity fit is dominant.
2. Modular products are configured to the closest supported usable capacity.
3. Under-sizing and over-sizing are shown explicitly.
4. Optional power, phase, backup, and inverter fields improve context but do
   not silently reject products when unpublished.
5. `Compatible` and `Not compatible` should only be used when both catalogue
   and household facts support that claim. Most products remain `Likely
   compatible`, `Requires installer confirmation`, `Additional
   inverter/controller required`, or `Unknown`.
6. Indicative product payback uses existing simulated annual savings and
   catalogue cost ranges. It does not rerun battery dispatch for products.

Capacity-fit labels use fixed thresholds: `Excellent fit` is within 5% or
0.75 kWh of the modelled capacity; `Close fit` is within 15% or 2.0 kWh;
larger differences are labelled as smaller/larger than recommended unless the
difference exceeds 40%, where the product is a `Poor capacity fit`.

The API exposes safe catalogue summaries through `battery_catalogue` and
product matches through `battery_product_matches`. Local paths and sensitive
provider details are not exposed. Quote-comparison fields are present as stable
seeds only; full installer quote entry is intentionally out of scope.

## Confidence Rules

Confidence is attached to recommendations and scenario results.

- **High**: SolarEdge interval data is present for most of the period and EV
  charging uses complete actual Ocular sessions.
- **Medium**: SolarEdge interval data is present, but EV charging is estimated
  or based on partial/session-snapshot data such as Home Assistant OCPP.
- **Low**: interval data is missing, sparse, or the billing period is not
  configured clearly enough for a reliable period estimate.

Projection confidence can use stricter day-count rules where appropriate:
fewer than 10 reading days is low, 10-20 days is medium, and 21+ days is high.

Every model should carry source metadata where available:

- `source`: machine-readable source name, such as `solaredge`,
  `ocular_export`, `home_assistant_ocpp`, or `estimated`.
- `source_record_id`: external record/session id when available.
- `source_confidence`: `high`, `medium`, or `low`.
- `source_imported_at`: local timestamp for when PowerMonitor observed or
  imported the source record.

## energy_interval

The normalized interval record for time-bucketed energy analytics.

Required fields:

- `start_time`
- `end_time`
- `duration_minutes`
- `production_kwh`
- `consumption_kwh`
- `grid_import_kwh`
- `grid_export_kwh`
- `tariff_bucket`

Optional fields:

- `grid_import_w`
- `production_w`
- `consumption_w`
- `solar_to_home_kwh`
- `solar_to_grid_kwh`
- `grid_to_home_kwh`
- `battery_charge_kwh`
- `battery_discharge_kwh`

Source fields:

- `source`
- `source_record_id`
- `source_imported_at`
- `source_confidence`

## solar_interval

A solar-focused view derived from `energy_interval`.

Required fields:

- `start_time`
- `end_time`
- `solar_generated_kwh`
- `solar_used_directly_kwh`
- `solar_exported_kwh`

Optional fields:

- `self_consumption_pct`
- `export_pct`
- `inverter_id`

Source fields:

- `source`
- `source_record_id`
- `source_imported_at`
- `source_confidence`

## grid_interval

A grid-focused view derived from `energy_interval`.

Required fields:

- `start_time`
- `end_time`
- `grid_import_kwh`
- `grid_export_kwh`
- `tariff_bucket`

Optional fields:

- `grid_import_w`
- `import_cost`
- `export_credit`
- `peak_flag`

Source fields:

- `source`
- `source_record_id`
- `source_imported_at`
- `source_confidence`

## ev_session

A normalized charging session. Complete session exports such as Ocular should
produce high-confidence sessions. Snapshot sources such as Home Assistant OCPP
may produce medium-confidence sessions if the beginning of the session was not
observed.

For Ocular spreadsheet imports, `start_time` and `end_time` preserve the raw
connection and disconnection timestamps from the source export. PowerMonitor
derives estimated active charging start/finish values at import and query time
from energy, configured charger power, and the charging schedule, and stores
those diagnostics in `notes_json` so historical rows remain compatible.

Required fields:

- `transaction_id`
- `start_time`
- `end_time`
- `energy_kwh`
- `source`
- `status`

Optional fields:

- `last_seen`
- `duration_minutes`
- `max_power_kw`
- `cost_estimate`
- `cost_partial`
- `tariff_breakdown_json`
- `super_off_peak_kwh`
- `super_off_peak_cost`
- `overnight_kwh`
- `overnight_cost`
- `off_peak_kwh`
- `off_peak_cost`
- `peak_kwh`
- `peak_cost`
- `start_source`
- `stop_source`
- `notes_json`

Ocular `notes_json` may include:

- `connected_at`
- `disconnected_at`
- `connected_duration`
- `connected_duration_minutes`
- `estimated_active_start`
- `estimated_active_finish`
- `estimated_active_duration_minutes`
- `estimated_active_duration_hours`
- `configured_charger_power_kw`
- `configured_schedule_start`
- `configured_schedule_end`
- `active_interval_inferred`
- `inference_note`
- `inference_diagnostics`

Source fields:

- `source`
- `source_record_id` or `transaction_id`
- `source_imported_at`
- `source_confidence`

## tariff_plan

The normalized representation of a tariff assumption.

Required fields:

- `name`
- `import_rates`
- `supply_charge_per_day`
- `export_credit_rate`

Optional fields:

- `plan_type`
- `effective_from`
- `effective_to`
- `comparison_flat_import_rate`
- `notes`

Source fields:

- `source`
- `source_record_id`
- `source_imported_at`
- `source_confidence`

## bill_period

The normalized billing/charge period used for reports and scenarios.

Required fields:

- `name`
- `start_date`
- `end_date`

Optional fields:

- `actual_bill_amount`
- `plan_name`
- `supply_days`
- `notes`

Source fields:

- `source`
- `source_record_id`
- `source_imported_at`
- `source_confidence`

## recommendation

A source-agnostic insight generated from normalized summaries.

Required fields:

- `id`
- `title`
- `severity`
- `summary`
- `action`
- `confidence`

Optional fields:

- `metrics`
- `period_start`
- `period_end`
- `source_inputs`

Source fields:

- `source`: usually `powermonitor_recommendation_engine`
- `source_confidence`

## scenario_result

A source-agnostic result from the What If engine.

Required fields:

- `id`
- `title`
- `estimated_bill`
- `difference_vs_actual`
- `savings`
- `loss`
- `confidence`

Optional fields:

- `description`
- `assumptions`
- `period_start`
- `period_end`
- `source_inputs`

Source fields:

- `source`: usually `powermonitor_scenario_engine`
- `source_confidence`
