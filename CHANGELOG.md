# Changelog

## Housekeeping - Configurable Web Host

- Made the embedded web dashboard host and port configurable through
  `POWMONITOR_HOST` and `POWMONITOR_PORT`.
- Added config-file fallback support via `web_server.host` /
  `web_server.port` or flat `web_host` / `web_port` keys.
- Preserved the default development bind of `127.0.0.1:8765`.
- Updated the LaunchAgent example to expose `0.0.0.0` for LAN appliance use
  without editing tracked source code.
- Added regression tests for defaults, config fallback, environment override,
  and invalid port handling.

## PB-004 - Capability Detection

- Added household capability detection for solar, EV, battery placeholder,
  grid/smart meter, tariff, imports, live/local collector, and data freshness.
- Exposed a Data Engine household capability summary and serialized it through
  the dashboard API.
- Made `AdvisorCollection` capability-aware so Home, Briefing, Goals, and the
  Recommendation Centre only activate advisors supported by detected household
  data.
- Added System page capability status and Imports page capability hints for
  Ocular EV upload availability.
- Added a data-quality recommendation for missing tariff setup.
- Added capability detection, advisor activation, API serialization, and setup
  recommendation regression tests.

## PB-003 - Import Framework and Web Upload

- Added shared household-scoped import contracts for jobs, sources, previews,
  results, and validation issues.
- Adapted the existing Ocular EV charging history importer behind the new
  plugin-style importer interface while preserving the command-line import.
- Added Data Engine import job persistence and scoped import history for the
  active household.
- Added an Imports page with browser upload, preview, commit, and privacy-safe
  success/failure pages for Ocular workbook exports.
- Added importer framework and web upload regression tests, including unknown
  format rejection and household-scoped EV session writes.

## PB-002 - Household Scoped Persistence

- Added household scoping migration for persisted household data including
  readings, EV sessions, timeline entries, recommendation lifecycle state, and
  billing-period records.
- Seeded the default local household/account context and migrated legacy rows
  to `default_household` for single-household compatibility.
- Scoped Data Engine reads and writes by active household context to prevent
  cross-household data leakage.
- Updated importer write paths to persist new readings and EV sessions into the
  default household.
- Added regression tests for legacy-row migration, household isolation, and API
  household metadata.

## PB-001 - Multi Household Foundation

- Added shared multi-household platform models: `Account`, `Household`,
  `HouseholdCapability`, `HouseholdMembership`, and `HouseholdContext`.
- Added default household context ownership to `DataEngine`, including local
  account, household, membership, and capability table seeding.
- Made active engines household-aware through optional household context while
  preserving existing advisor and calculation behaviour.
- Scoped `AdvisorCollection`, Briefing Engine, Goals Engine, Recommendation
  Engine, and Decision Engine outputs to household context where available.
- Added household, account, and membership metadata to dashboard API
  serialization.
- Added multi-household foundation tests for model serialization, default
  household seeding, and household-scoped orchestration.

## Sprint 1.7.0 - Goals Engine

- Added reusable `GoalsEngine` for tracking household progress from
  `AdvisorCollection` outputs only.
- Added shared `Goal`, `GoalProgress`, and `GoalStatus` models for API-safe
  goal reporting.
- Added v1 active goals for bill target, Peak reduction, solar usage,
  off-peak EV charging, and overnight baseload.
- Added Home Daily Briefing `Today's Progress` cards showing goal progress,
  confidence, and advisor-supported status.
- Added Recommendation Centre goal contribution labels for recommendations
  that support active goals.
- Added Goals Engine and Recommendation Centre goal-rendering tests.

## Sprint 1.6.0 - Advisor Orchestration

- Enhanced `BriefingEngine` to cluster related advisor signals into one
  household narrative rather than repeating separate advisor messages.
- Added deterministic orchestration scoring using urgency, measurable
  financial impact, confidence, and estimated action effort.
- Daily Briefing now emits one `Today's Best Action`, one `You're Doing Well`
  reinforcement, and one `Watch This` item where advisor data supports them.
- Added supporting-advisor metadata to briefing priorities so merged actions
  can show which advisors agree.
- Kept individual advisor outputs unchanged for Recommendation Centre and API
  advisor summaries.
- Added orchestration tests for EV/cost merging, solar/efficiency merging, and
  briefing item caps.

## Sprint 1.5.0 - Efficiency Advisor

- Added reusable `EfficiencyAdvisor` that answers where household energy may be
  wasted using measured overnight baseload, daily import trends, Financial
  Engine, Recommendation Engine, timeline, and freshness data.
- Added Efficiency Advisor to `AdvisorCollection`, so Home, Recommendation
  Centre, API, and Briefing Engine consume it automatically.
- Added efficiency statuses: `EXCELLENT`, `GOOD`, `WATCH`, `INEFFICIENT`, and
  `UNKNOWN`.
- Added evidence trail items for overnight baseload, daily average import,
  previous-period trend, financial impact estimate, recommendation context,
  daily import trends, and timeline evidence.
- Added Efficiency Advisor unit tests.

## Sprint 1.4.0 - Solar Advisor

- Added reusable `SolarAdvisor` that answers whether the household is making
  the most of available solar using existing period-level solar and financial
  data.
- Added Solar Advisor to the shared Advisor framework, dashboard API, Home,
  Recommendation Centre, and Briefing Engine through `AdvisorCollection`.
- Added solar advisor statuses: `EXCELLENT`, `GOOD`, `OPPORTUNITY`,
  `MISSED_OPPORTUNITY`, and `UNKNOWN`.
- Added evidence trail items for solar generation, self-consumption, exported
  energy, export credit, import offset, billing contribution, recommendations,
  and timeline context.
- Added Solar Advisor unit tests.

## Sprint 1.3.0 - Briefing Engine

- Added reusable `BriefingEngine` that orchestrates `AdvisorCollection` into a
  concise Home daily briefing.
- Added shared `DailyBriefing`, `BriefingSection`, and `BriefingPriority`
  models for advisor-driven briefing output.
- Refactored Home to render the Briefing Engine output instead of assembling
  its own summary from decisions or advisor-specific objects.
- Added Daily Briefing API serialization via `daily_briefing`.
- Added Briefing Engine tests for advisor-only orchestration, section caps, and
  serialization.

## Sprint 1.2.0 - Cost Advisor

- Added a reusable `CostAdvisor` that answers whether the household is on
  track financially using existing Financial Engine projection and billing
  comparison data.
- Added Cost Advisor output to the shared `Advisor` framework, dashboard API,
  Home, and Recommendation Centre.
- Added financial evidence trail items for projected bill, previous-period
  comparison, import/export movement, EV contribution, tariff contribution,
  recommendations, and timeline context.
- Expanded shared advisor statuses for cost states: `ON_TRACK`,
  `SLIGHTLY_HIGH`, `HIGH`, `IMPROVING`, and `UNKNOWN`.
- Added Cost Advisor unit tests.

## Sprint 1.1.0 - Advisor Framework

- Added shared `Advisor` and `AdvisorCollection` contracts for future
  presentation-agnostic advisors.
- Refactored Charging Advisor to expose its existing behaviour through the
  shared `Advisor` contract without changing charge/wait/avoid logic.
- Added advisor aggregation to the dashboard API and report generation path.
- Updated Home, Glance Mode, and Recommendation Centre surfaces to consume
  shared `Advisor` fields where appropriate.
- Added Advisor contract, collection ordering, and Charging Advisor parity
  tests.

## Sprint 1.0.2 - Advisor Evidence

- Added shared `EvidenceItem` and `EvidenceTrail` models for ordered advisor
  reasoning.
- Added `evidence_trail` to `ChargingAdvice` with tariff, solar, EV-session,
  recommendation-context, and final recommendation evidence items.
- Added a compact expandable `Why?` section to the Home Charging Advisor card.
- Kept Charging Advisor recommendation logic unchanged while making the
  reasoning auditable through the API and report.
- Added Evidence Trail model, serialization, advisor ordering, and rendering
  tests.

## Sprint 1.0.1 - Charging Advisor Context

- Expanded `ChargingAdvice` with current and next tariff-window context,
  tariff labels/rates, next-window start time, per-kWh difference, data-status
  fields, and confidence reasoning.
- Improved Charging Advisor explanations for charge, wait, avoid, and unknown
  states with clearer tariff-based versus daily solar/export-based wording.
- Kept solar advice grounded in daily interval/export evidence without implying
  live instantaneous surplus or charger state.
- Updated Home and Glance Mode to consume the richer shared `ChargingAdvice`
  object.
- Added tests for next-window metadata, per-kWh difference, and confidence
  reasoning.

## Sprint 1.0.0 - Charging Advisor

- Added a shared `ChargingAdvice` model and reusable `ChargingAdvisor` engine
  for answering whether EV charging should happen right now.
- Combined existing tariff-window, solar export, bill-position, recommendation,
  EV history, and freshness evidence without adding forecasts or new
  collectors.
- Refactored Glance Mode to consume `ChargingAdvice` instead of owning
  charge/wait/avoid decision rules.
- Added a Home Charging Advisor block and API `charging_advice` payload sourced
  from the shared advisor object.
- Added unit coverage for Charging Advisor decision rules and updated Glance
  adapter tests.

## Sprint 0.9.0 - Glance Mode

- Added Glance Mode for garage, study, kitchen, and kiosk/tablet displays.
- Added `#glance` to the generated static dashboard and `/glance` to the local
  web app.
- Rendered a one-screen wall-display view with large `CHARGE NOW`, `WAIT`,
  `AVOID`, and `DATA UNAVAILABLE` states.
- Reused existing Decision Engine, Energy Score, Financial Engine, tariff,
  solar, bill, and freshness outputs with only presentation-layer state
  mapping.
- Added tests for Glance status mapping and current tariff-window detection.

## Sprint 0.8.3 - Recommendation Controls

- Added Recommendation Centre controls to dismiss, snooze, and restore
  recommendations.
- Added local web POST handling for recommendation lifecycle updates backed by
  `DataEngine` persistence.
- Added snooze options for 1 day, 7 days, and next billing period where
  available.
- Added a hidden recommendations review section with restore controls.
- Added tests for rendered controls and local web dismiss/snooze/restore
  interactions.

## Sprint 0.8.2 - Recommendation Lifecycle v1

- Added SQLite-backed recommendation lifecycle state keyed by stable
  recommendation ID.
- Added active, dismissed, and snoozed-until lifecycle handling owned by
  `DataEngine`.
- Updated the Recommendation Engine to apply lifecycle state and filter
  dismissed/currently snoozed recommendations from active outputs by default.
- Added Recommendation Centre hidden counts for dismissed and snoozed
  recommendations.
- Added tests for lifecycle persistence, filtering, expired snoozes, and Centre
  hidden-count rendering.

## Sprint 0.8.1 - Recommendation Centre

- Added a Recommendations dashboard page that ranks all active household
  opportunities by Recommendation Engine priority score.
- Rendered category, score, financial impact, confidence, difficulty,
  supporting evidence, recommended action, and Why? explanation for each
  recommendation.
- Kept recommendation generation centralized in the shared Recommendation
  Engine and reused existing Decision adapter output for presentation.
- Added regression coverage for Recommendation Centre sorting and rendered
  fields.

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
