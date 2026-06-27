# PowerMonitor Vision

PowerMonitor is evolving from a local bill estimator into a household energy
intelligence platform.

## Mission

Help households understand, predict, and reduce energy costs using real usage
data.

## Product Boundaries

PowerMonitor is not:

- A Home Assistant replacement
- A smart home control platform
- A generic graphing dashboard
- A solar monitoring clone

PowerMonitor is:

- A bill explanation engine
- A tariff and plan comparison tool
- A decision assistant
- A scenario and ROI modelling platform
- A connector-driven analytics layer above SolarEdge, Ocular, Home Assistant,
  retailer data, CSV imports, and future sources

## Product Pillars

### 1. Explain

Answer: Why was my bill this much?

PowerMonitor should translate usage, tariffs, supply charges, exports, billing
periods, and source quality into a clear financial explanation. The user should
be able to see the drivers behind a bill without needing to inspect raw interval
data.

### 2. Predict

Answer: What will happen if nothing changes?

PowerMonitor should project likely costs from current patterns, known tariffs,
seasonality, and data confidence. Forecasts should make uncertainty visible and
help households spot cost problems before the bill arrives.

### 3. Recommend

Answer: What should I do today?

PowerMonitor should turn analytics into practical, timely actions. A good
recommendation is specific, financially meaningful, and grounded in the user's
actual household data.

### 4. Decide

Answer: What should I buy or change?

PowerMonitor should support higher-stakes decisions such as tariff changes,
battery purchases, EV charging behaviour, appliance upgrades, and solar export
strategy. Scenario and ROI modelling should help users compare options before
spending money or changing plans.

## Product Principles

- Every feature must help answer a financial or behavioural energy decision.
- Data sources are interchangeable; the core analytics engine is source-agnostic.
- Confidence must be visible when data is incomplete.
- Prefer decision cards over raw graphs.
- Prefer dollar impact over abstract kWh where practical.
- Home page should answer "Am I doing okay?"
- System page should answer "Can I trust the data?"

## Roadmap

### 0.5 Core Analytics

Build the reliable source-agnostic analytics foundation: normalized usage,
tariff buckets, billing periods, cost attribution, data quality, and privacy-safe
local reporting.

### 0.6 Decision Engine

Convert analytics into ranked decision cards, recommendations, tariff
comparisons, and plain-English bill explanations.

### 0.7 Forecasting

Add projections for current and future billing periods, expected cost trends,
seasonal patterns, and visible confidence bands when source data is incomplete.

### 0.8 Battery Simulator

Model battery size, charge/discharge behaviour, export tradeoffs, payback
periods, and household-specific ROI using real usage and tariff data.

### 0.9 Connector Platform

Formalize connectors for SolarEdge, Ocular, Home Assistant, retailer data, CSV
imports, and future sources while keeping the analytics engine independent of
source-specific details.

### 1.0 Polished Energy Intelligence Product

Deliver a cohesive product experience focused on household energy decisions,
with clear trust signals, financial impact, reliable forecasts, and a polished
local-first interface.
