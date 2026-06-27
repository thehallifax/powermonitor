# Changelog

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
