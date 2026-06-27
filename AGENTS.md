# AGENTS.md

Repository-level guide for AI agents working on PowerMonitor.

## Product Mission

PowerMonitor helps households make better energy decisions through explainable financial intelligence.

PowerMonitor is not:
- A Home Assistant replacement.
- A smart home control platform.
- A generic graphing dashboard.
- A solar monitoring clone.

PowerMonitor is:
- A bill explanation engine.
- A tariff and plan comparison tool.
- A decision assistant.
- A scenario and ROI modelling platform.
- A connector-driven analytics layer above SolarEdge, Ocular, Home Assistant, retailer data, CSV imports, and future sources.

## Product Principles

- Decisions before data.
- Explain everything.
- Financial impact first.
- Confidence is mandatory.
- Vendor neutral.
- One question per screen.
- Use calm, practical, non-alarmist language.
- Never show data without explaining why it matters.

## Architecture Rules

These rules are strict:

- Connectors acquire data only.
- `DataEngine` owns SQLite/database access.
- `FinancialEngine` owns financial calculations.
- `DecisionEngine` owns recommendation/decision logic.
- Presentation layers render engine outputs only.
- Do not duplicate calculations.
- Prefer typed platform models over loose dictionaries.
- Every recommendation/decision must include finding, evidence, financial impact, recommendation, confidence, and data sources used.
- Do not introduce new connectors unless explicitly requested.
- Do not redesign the architecture without calling it out clearly.

## Current Architecture

Current flow:

```text
Connectors/importers
-> DataEngine
-> FinancialEngine
-> DecisionEngine
-> Presentation/API/reports
```

Key files:

- `engine/models.py` contains shared contracts.
- `engine/data_engine.py` is the source-of-truth access layer.
- `engine/financial_engine.py` owns money.
- `engine/decision_engine.py` owns decision generation.
- `collector/report.py` is currently still the main presentation/report generator and contains some legacy logic to migrate gradually.

## Development Workflow

Every sprint must include:

- Mission.
- Objective.
- Scope.
- Validation.
- Git commit.
- Push.
- `CHANGELOG.md` update.
- Sprint review:
  1. What changed?
  2. Why is the architecture/product better?
  3. What technical or UX debt intentionally remains?
  4. What should the next sprint focus on?

## Validation Expectations

- Run `py_compile` on touched Python files.
- Run unit tests.
- Run `collector/report.py` when presentation/reporting could be affected.
- Confirm dashboard/API behaviour is unchanged unless intentionally modified.
- Run a privacy scan for secrets, cookies, and tokens.
- Do not commit runtime data, logs, reports, secrets, or local databases.

## Git Expectations

- Commit and push every meaningful sprint.
- If commit or push fails, stop and report the error.
- Use tags for meaningful platform milestones.
- Keep generated/runtime files uncommitted.

## Current Sprint Focus

- Sprint 0.6.4 Daily Briefing.
- Improve Home into a narrative daily briefing.
- Continue consuming `DataEngine`, `FinancialEngine`, and `DecisionEngine` outputs.
- Do not add new calculations unless explicitly requested.
- Do not add new connectors.

## Links

- `README.md`
- `CHANGELOG.md`
- `docs/VISION.md`
- `docs/UX_ROADMAP.md`
- `docs/INTELLIGENCE_ROADMAP.md`
- `docs/data-model.md`
