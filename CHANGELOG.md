# Changelog

## Sprint 0.7.5 - Platform Stabilisation

- Added API-safe serialization for shared engine and presentation models before
  JSON responses are emitted.
- Stabilized `/api/summary` JSON output when nested decision, timeline, and
  energy score models are present.
- Updated EV daily trend chart data to use persisted `ev_sessions` totals
  grouped by `date(start_time)` through the Data Engine.
- Added regression tests for API serialization and EV daily trend totals.

## Sprint 0.7.4 - Energy Score v1

- Added an `EnergyScore` model with score, label, summary, positive/negative
  factors, potential score, confidence, sources, and explanation.
- Added `presentation/energy_score.py` to score existing summary, decision,
  data completeness, plan comparison, and trend-aware decision outputs.
- Exposed Energy Score in `/api/summary`.
- Added a compact Energy Score card to the Home Daily Briefing with factor
  lists, potential score, and explainability details.
- Added tests for scoring bands, potential score, confidence, and briefing
  serialization.

## Sprint 0.7.3 - Trend Recency and Decay

- Added recency-aware trend analysis for persisted timeline history consumed by
  `DecisionEngine`.
- Added support for last-7-days, last-14-days, and current-billing-period
  trend windows.
- Added decayed event scoring so older timeline events contribute less than
  newer events.
- Suppressed stale trend decisions when historical events have no recent
  supporting evidence.
- Updated trend-aware decisions to include recent count, historical count,
  recency window, confidence reason, and explanation text for why the trend
  matters now.
- Added tests for trend decay and stale trend suppression.

## Sprint 0.7.2 - Trend-Aware Decisions

- Updated `DecisionEngine` to consume persisted timeline history through
  `DataEngine`.
- Added trend-aware decisions for repeated high peak usage, improving/worsening
  bill trajectory, recurring EV charging wins, recurring solar utilisation
  wins, and repeated estimated/fallback data warnings.
- Kept trend logic limited to simple counts and comparisons over existing
  persisted timeline entries.
- Added tests for trend-aware decisions and DataEngine-backed timeline context.

## Sprint 0.7.1 - Persistent Timeline

- Added SQLite-backed timeline storage owned by `DataEngine`.
- Added idempotent timeline entry upserts keyed by stable event identity to
  avoid duplicate events across report runs.
- Added historical timeline querying by date range.
- Preserved timeline timestamp, title, summary, financial impact, confidence,
  evidence sources, and event type in storage and serialized output.
- Updated report/API timeline rendering to consume persisted timeline entries
  after candidate events are derived from existing outputs.
- Added persistence tests for idempotency and period querying.

## Sprint 0.7.0 - Decision Timeline

- Added `DecisionTimeline` and `TimelineEntry` shared models for chronological
  household decision memory.
- Added `presentation/timeline.py` to build timeline entries from existing
  billing period metadata, period comparisons, and Decision Engine output.
- Added a Timeline dashboard page with human-readable entries, financial impact
  where known, confidence, and evidence sources.
- Exposed serialized timeline entries in `/api/summary`.
- Added timeline unit tests for chronological ordering, decision-derived events,
  bill tracking outcomes, and serialized entry shape.

## Sprint 0.6.6 - Explainability Everywhere

- Rendered compact `Why?` explanation sections across Decision surfaces,
  including Daily Briefing cards, Top Actions, and Recommendation cards.
- Reused Decision Engine-owned `DecisionExplanation` payloads for why a
  decision matters, evidence, financial impact, confidence reason, data
  sources, and freshness/completeness notes.
- Updated `/api/summary` recommendation output to serialize decisions through
  the shared `Decision.as_dict()` contract, including explanation data.
- Added report-level tests for rendered explanations and serialized
  explanation fields.

## Sprint 0.6.5 - Explainability

- Added Decision explanation payloads for why a decision matters, evidence
  used, financial impact, confidence reason, data sources, and
  freshness/completeness notes where available.
- Updated the Decision Engine to populate explanations from existing decision
  output without adding new calculations or new decision logic.
- Extended the Daily Briefing presentation model so priorities and
  opportunities can expose explanation details.
- Updated the Home Daily Briefing priority card with a compact `Why?`
  expandable explanation section.
- Added unit tests for explanation serialization and Daily Briefing explanation
  output.

## Sprint 0.6.4 - Daily Briefing

- Added `presentation/briefing.py` as a lightweight Output Layer that consumes
  Decision Engine `Decision` objects and produces a Home-page Daily Briefing.
- Updated Home to render a calm narrative briefing with a greeting, today's
  priority, today's win, up to three opportunities, read time, confidence, and
  source attribution.
- Kept briefing selection and wording in the presentation layer without adding
  data access, financial calculations, connectors, or new recommendation logic.
- Added unit tests for Daily Briefing selection, formatting, read-time, and
  source attribution behaviour.

## Sprint 0.6.3 - Financial Impact Engine

- Added `engine/financial_engine.py` as the owner for bill projection, import
  cost, export credit, net bill, EV session cost, tariff comparison, and
  monthly/annual projection arithmetic.
- Migrated representative financial logic from `collector/report.py` into the
  Financial Engine while preserving existing dashboard and report values.
- Updated the Decision Engine to consume `FinancialImpact` objects produced by
  the Financial Engine for projected bills and representative savings cases.
- Added basic unit tests for Financial Engine calculations and tariff
  comparison output shape.

## Sprint 0.6.2 - Data Engine

- Added `engine/data_engine.py` as the platform owner for SQLite-backed
  household data retrieval, freshness, completeness, connector status, and
  normalization.
- Migrated representative report data access for database validation, EV
  sessions, EV source counts, connector status, and overnight baseload reads
  behind the Data Engine.
- Added a Data Engine-aware Decision Engine context builder while preserving
  existing decision outputs.
- Added basic unit tests for Data Engine validation and normalized data access.

## Sprint 0.6.1 - Engine Contracts

- Added shared intelligence engine models for decisions, evidence, confidence,
  freshness, financial impact, insights, forecasts, and engine status.
- Added placeholder Financial, Behaviour, Prediction, and Optimisation engine
  modules with minimal importable interfaces and no business logic.
- Updated the Decision Engine to consume the shared model contracts while
  preserving existing report output shape.
- Added basic model validation tests.
