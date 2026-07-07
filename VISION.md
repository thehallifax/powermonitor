# PowerMonitor Vision

## Mission

PowerMonitor helps households understand energy costs, compare choices, and make
better decisions using their own local energy data.

## Primary Goal

The primary goal is to turn raw solar, grid, tariff, billing, and EV charging
data into clear explanations and practical decisions. PowerMonitor should help a
user see what is driving their bill, what is likely to happen next, and what
change would matter most.

## Target Audience

PowerMonitor is for households with solar, EV charging, time-of-use tariffs, or
complex energy bills who want more than a monitoring graph. It is also for
technical contributors who want to connect new data sources while keeping the
analytics and recommendations source-agnostic.

## Core Design Principles

- Local-first: data should remain on the user's machine unless they explicitly
  choose otherwise.
- Decision-first: prioritize explanations, tradeoffs, and actions over raw
  charts or dense tables.
- Source-agnostic: collectors and importers may be source-specific, but reports,
  scenarios, recommendations, and cost analysis should work from internal models.
- Confidence-aware: clearly show when data is complete, estimated, partial, or
  missing.
- Financially grounded: prefer dollar impact and bill attribution where they help
  users make decisions.
- Calm and clear: use generous presentation, plain language, and progressive
  disclosure.

## Key Questions

PowerMonitor should help users answer:

- How am I doing today?
- Where did my money go this billing period?
- How well is my solar performing?
- What did my vehicle cost to charge?
- Is my current tariff plan helping or hurting?
- What should I change next?
- Can I trust the data behind these answers?

## What PowerMonitor Is Not

PowerMonitor is not trying to replace Home Assistant, inverter portals, retailer
apps, or general-purpose graphing dashboards. Those systems can remain useful
data sources. PowerMonitor's role is narrower: explain household energy costs,
model scenarios, compare plans, and support decisions.

## Guiding Principle

Every new feature should help users make better home energy decisions. If a
feature does not improve understanding, trust, actionability, or decision quality,
it should be simplified, deferred, or left out.
