# Changelog

## Sprint 0.6.1 - Engine Contracts

- Added shared intelligence engine models for decisions, evidence, confidence,
  freshness, financial impact, insights, forecasts, and engine status.
- Added placeholder Financial, Behaviour, Prediction, and Optimisation engine
  modules with minimal importable interfaces and no business logic.
- Updated the Decision Engine to consume the shared model contracts while
  preserving existing report output shape.
- Added basic model validation tests.
