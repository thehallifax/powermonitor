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

