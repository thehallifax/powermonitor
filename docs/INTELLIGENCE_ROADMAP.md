# PowerMonitor Intelligence Roadmap

This roadmap describes planned intelligence work for making PowerMonitor
recommendations smarter, more explainable, and financially grounded. It is a
planning document only; no feature implementation is included here.

## Goal

PowerMonitor should turn normalized energy, billing, tariff, EV, solar, and
future weather/load data into decisions that are clear enough to act on. A good
recommendation should explain what was found, what evidence supports it, what
the likely financial impact is, how confident PowerMonitor is, and which data
sources were used.

The intelligence engine should remain source-agnostic. Connectors and importers
continue to handle SolarEdge, Home Assistant, Ocular, tariff files, weather
sources, and future integrations. Recommendation logic should consume normalized
models and source metadata rather than source-specific records.

## Recommendation Structure

Each recommendation should use the following structure.

- **Finding**: the observed condition, pattern, anomaly, or opportunity.
- **Evidence**: the measurements, comparisons, periods, and thresholds that
  support the finding.
- **Financial impact**: estimated cost, savings, bill risk, payback, or range of
  possible outcomes.
- **Recommendation**: the suggested user action, including timing or operating
  conditions where relevant.
- **Confidence**: high, medium, or low confidence, with a short reason.
- **Data sources used**: normalized models and source metadata used to produce
  the recommendation.

Example shape:

```json
{
  "finding": "EV charging frequently overlaps peak tariff periods.",
  "evidence": [
    "18.4 kWh charged during peak periods in the current billing period",
    "Peak charging represented 22% of observed EV charging energy"
  ],
  "financial_impact": {
    "estimated_monthly_savings": 14.80,
    "currency": "AUD",
    "basis": "Shift peak EV charging to super off-peak tariff buckets"
  },
  "recommendation": "Move scheduled EV charging to start after the super off-peak window begins.",
  "confidence": {
    "level": "medium",
    "reason": "Interval energy data is complete, but EV attribution depends on imported session coverage."
  },
  "data_sources_used": [
    "energy_interval",
    "ev_session",
    "tariff_plan",
    "bill_period"
  ]
}
```

## Planned Intelligence Work

### Evidence-Based Recommendations

Recommendations should be traceable to metrics rather than text-only heuristics.
Each recommendation should carry enough evidence for the dashboard to show why
it appeared and enough metadata for tests to verify that it is reproducible.

Planned capabilities:

- Define evidence objects for intervals, periods, sessions, tariffs, and
  comparisons.
- Record thresholds and comparison baselines used by each rule.
- Attach source inputs so recommendations can be audited without exposing
  credentials or raw private values.
- Separate finding generation from presentation copy.

### Financial Impact Engine

The financial impact engine should calculate monetary impact for each insight
using tariff-aware interval costs, supply charges, export credits, and scenario
assumptions.

Planned capabilities:

- Estimate savings or costs from load shifting, export changes, EV charging
  timing, tariff switching, and battery behavior.
- Return ranges when the inputs are incomplete or forecast-based.
- Distinguish observed bill impact from projected future impact.
- Explain assumptions such as tariff rates, billing period length, and
  replacement behavior.

### Energy Score Calculation

The Energy Score should summarize household energy performance without hiding
the underlying evidence. It should be a decision aid, not a vanity metric.

Candidate score components:

- Solar self-consumption.
- Peak import exposure.
- Super off-peak EV charging share.
- Export value capture.
- Forecast bill risk.
- Data completeness and confidence.

The score should include component scores and short explanations so users know
which behavior is helping or hurting the total.

### Bill Attribution Engine

Bill attribution should explain where the estimated bill came from. It should
break costs into understandable drivers instead of only showing a single net
number.

Planned attribution categories:

- Supply charge.
- Grid import by tariff bucket.
- Solar export credits.
- EV charging cost by tariff bucket.
- Estimated household non-EV load.
- Solar offset value.
- Residual or unattributed cost where data is incomplete.

### Battery Simulator

The battery simulator should estimate how a battery would change imports,
exports, self-consumption, and bills under realistic operating assumptions.

Planned simulation inputs:

- Battery size and usable capacity.
- Charge and discharge limits.
- Round-trip efficiency.
- Reserve state of charge.
- Operating mode, such as self-consumption, tariff arbitrage, or backup-first.
- Existing solar generation and household load intervals.

Outputs should include estimated bill impact, avoided peak imports, reduced
exports, cycle estimates, confidence, and key assumptions.

### Tariff Comparison Expansion

Tariff comparisons should grow from simple plan checks into a broader engine for
evaluating current and alternative plans against actual usage.

Planned capabilities:

- Compare multiple import/export/supply-charge structures over the same period.
- Support effective dates and plan versioning.
- Show winner by total bill, import cost, export value, and EV charging cost.
- Identify plan sensitivity, such as whether savings depend on EV behavior or
  solar export volume.
- Preserve all tariff assumptions used in each comparison.

### Weather-Aware Solar Forecasting

Weather-aware forecasting should improve bill projections and solar-related
recommendations by separating normal solar performance from weather-driven
variation.

Planned capabilities:

- Incorporate daily weather features such as cloud cover, temperature, rain, or
  solar irradiance when available.
- Compare observed solar production against expected production for similar
  conditions.
- Improve short-term bill projections using forecast weather.
- Flag low-confidence forecasts when weather data is missing or highly variable.

### Appliance and Load Attribution

Appliance/load attribution should estimate which loads are contributing to cost
or peak demand. It should start conservative and clearly label inferred results.

Potential attribution sources:

- EV sessions from Ocular or Home Assistant OCPP.
- SolarEdge interval consumption and import patterns.
- Known schedules for recurring loads.
- Future Home Assistant device sensors.
- User-supplied appliance metadata.

Initial outputs should distinguish measured loads from inferred loads and avoid
overstating precision.

### Confidence Scoring

Confidence should be calculated consistently across recommendations, scenarios,
forecasts, and financial estimates.

Confidence factors:

- Interval data completeness.
- Billing period configuration quality.
- Source confidence from normalized records.
- EV session completeness.
- Forecast horizon.
- Whether the result is observed, estimated, inferred, or simulated.
- Sensitivity of the result to uncertain assumptions.

Confidence should include both a machine-readable level and a human-readable
reason.

### Decision Assistant

The Decision Assistant should help users choose between actions, not just list
insights. It should rank recommendations by value, confidence, urgency, and ease
of action.

Planned capabilities:

- Group related recommendations into decisions, such as "change EV schedule",
  "compare tariffs", or "consider battery sizing".
- Show estimated upside, risk, prerequisites, and confidence.
- Avoid recommending actions when the data is too weak; suggest data collection
  improvements instead.
- Produce clear next steps that can be completed outside PowerMonitor.

## Engine Boundaries

The intelligence engine should:

- Consume normalized data models documented in `docs/data-model.md`.
- Keep source-specific parsing and credentials in connectors/importers.
- Avoid logging secrets, cookies, tokens, or raw credentials.
- Store source metadata and confidence, not private raw artifacts.
- Prefer explainable calculations over opaque model behavior for financial
  advice.
- Treat forecasts, attribution, and simulations as estimates unless directly
  observed.

## Validation Expectations

This roadmap is docs-only. Future implementation work should include its own
tests and privacy checks.

For this document change:

- No application code should change.
- No collector, dashboard, database, or report behavior should change.
- No credentials, cookies, tokens, account numbers, or private bill identifiers
  should be introduced.
- README linking is optional.
