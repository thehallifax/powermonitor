# PowerMonitor UX Roadmap

PowerMonitor should feel like a polished consumer product: calm, trustworthy,
easy to scan, and focused on decisions. The interface should help a household
understand energy, solar, EV charging, and bills without requiring them to
think like the underlying data model.

This roadmap is intentionally docs-only. It defines the user experience shape
for future implementation work, but it does not introduce new features yet.

## Product Principles

- Lead with the question the user came to answer.
- Turn raw readings into plain-language status, cost, confidence, and next
  actions.
- Prefer one strong primary insight per page over a wall of metrics.
- Show enough detail to earn trust, then let users progressively reveal deeper
  explanations.
- Make uncertainty visible without making the product feel broken.
- Keep privacy expectations explicit: local-first, no unnecessary external
  assets, and no credentials or personal usage data exposed in reports.
- Treat print/PDF output as a first-class product surface, not a browser
  afterthought.

## Page Questions

Each page should have one primary question. Layout, hierarchy, copy, and empty
states should all serve that question.

| Page | Primary question |
| --- | --- |
| Home | How am I doing today? |
| Bills | Where did my money go? |
| Solar | How well is my solar performing? |
| EV | What did my vehicle cost to charge? |
| What If | What should I change? |
| Analytics | What patterns exist? |
| System | Can I trust the data? |
| Glance | Can I charge or use flexible loads right now? |

## Planned UX Work

### Home Dashboard Refinement

The Home page should become the daily command center. It should answer whether
today is normal, better, or worse than expected, then offer the most useful next
step.

Planned direction:

- Create a clear top summary for today: cost, solar contribution, grid import,
  grid export, and EV charging where relevant.
- Make the current billing period visible without overwhelming the daily view.
- Surface the highest-value action or warning first.
- Reduce competing card weights so the page has an obvious reading order.
- Preserve fast scanning on desktop and mobile.

### Energy Score

Energy Score should provide a simple, explainable snapshot of household energy
performance for the selected period.

Planned direction:

- Use a stable score model that combines cost control, solar self-consumption,
  peak import avoidance, EV charging timing, and data confidence.
- Pair the score with a short explanation of why it moved.
- Avoid gamification that hides cost reality or overstates precision.
- Show data confidence next to the score when inputs are incomplete.

### Daily Briefing

Daily Briefing should translate the latest readings into a short narrative:
what happened, what matters, and what to do next.

Planned direction:

- Start with one plain-language status sentence.
- Include the most relevant cost, solar, EV, and anomaly notes.
- Keep it short enough to read before leaving the dashboard.
- Adapt when data is missing, stale, or low confidence.

### Decision Cards

Decision cards should turn analytics into practical recommendations.

Planned direction:

- Give each card a clear action, expected impact, confidence level, and reason.
- Distinguish between immediate actions, recurring habits, and tariff or
  system-level changes.
- Keep cards source-agnostic so recommendations can use SolarEdge, Ocular,
  Home Assistant, or future normalized inputs.
- Avoid presenting weak correlations as firm advice.

### Glance Mode / Garage Display

Glance Mode should provide a wall-display-friendly view for a garage, study,
kitchen, or tablet display. Its primary question is: can I charge or use
flexible loads right now?

Route direction:

- Support `/glance` when served by the local dashboard.
- Support `#glance` for the static hash-routed dashboard.
- Keep the route free of normal navigation clutter.

Planned direction:

- Use a one-screen layout suitable for kiosk and tablet display.
- Show one very large status: `CHARGE NOW`, `WAIT`, or `DO NOT CHARGE`.
- Explain the status with a short reason such as peak period, super off-peak
  window, solar surplus, or low confidence / missing data.
- Show the next best charging or flexible-load window.
- Show the current tariff bucket.
- Show the current and projected bill.
- Show solar status where available, including surplus when relevant.
- Auto-refresh so the display can stay open unattended.
- Use dark-mode-friendly styling with enough contrast for low-light rooms.
- Keep typography readable from several metres away.
- Avoid dense tables, multi-step controls, and secondary analytics.

Implementation constraint:

- Glance Mode must consume the same Decision Engine output as Home. It should
  present a display-specific view of shared decisions, not duplicate
  recommendation or tariff-window logic.

### Timeline View

Timeline view should help users understand when energy events happened and how
they relate to costs.

Planned direction:

- Show solar generation, grid import/export, tariff buckets, EV sessions, and
  notable events on a shared time axis.
- Support quick comparisons between today, yesterday, the billing period, and
  selected custom dates.
- Make sparse or estimated intervals visually distinct.
- Keep drill-down details available without crowding the main timeline.

### Better Mobile Layout

Mobile should feel intentionally designed, not merely compressed.

Planned direction:

- Prioritize the primary question and top action on each page.
- Use stacked summaries, compact navigation, and touch-friendly controls.
- Prevent tables and charts from forcing horizontal scanning where a summary
  or segmented view would be clearer.
- Keep long labels, confidence messages, and currency values readable at small
  widths.

### Print/PDF Polish

Print and PDF output should read like a clean household energy report.

Planned direction:

- Ensure all pages expand in a logical order for print.
- Avoid clipped charts, orphaned headings, and navigation-only UI in PDF mode.
- Include period, generated-at time, assumptions, and confidence notes.
- Preserve privacy by excluding credentials, cookies, raw tokens, and
  unnecessary local runtime details.

### Empty States and Data-Confidence Messaging

Empty states should explain what is missing, why it matters, and what the user
can do next.

Planned direction:

- Replace blank panels with specific states for no data, stale data, partial
  data, authentication failure, and unconfigured billing periods.
- Tie confidence levels to the model described in `docs/data-model.md`.
- Show whether a number is measured, estimated, projected, or unavailable.
- Keep recovery steps practical and privacy-safe.

### Progressive Disclosure

PowerMonitor should be approachable first and inspectable second.

Planned direction:

- Show headline answers before tables, formulas, and source details.
- Use expandable details for assumptions, tariff breakdowns, and confidence
  explanations.
- Keep advanced analytics available without making the default path feel
  technical.
- Let the System page carry deeper diagnostics while user-facing pages stay
  decision-focused.

## UX Validation

Before implementation work begins, validate proposed designs against these
checks:

- **Question fit**: each page clearly answers its primary question.
- **Scan test**: the most important status and action are visible within a few
  seconds.
- **Mobile test**: no core workflow depends on a wide screen.
- **Kiosk test**: Glance Mode is readable from several metres away, works in
  dark mode, auto-refreshes, and answers its primary question without
  navigation.
- **Print/PDF test**: the saved report is readable, complete, and uncluttered.
- **Confidence test**: estimated or incomplete data is labeled without alarm.
- **Privacy scan**: generated UI and reports do not expose secrets, cookies,
  tokens, credentials, or unnecessary personal raw data.
- **Progressive disclosure test**: advanced details are available, but not
  required for the default user journey.

## Non-Goals For This Roadmap

- Do not implement the Energy Score, Daily Briefing, decision cards, Glance
  Mode, timeline, or mobile changes yet.
- Do not change collectors, importers, normalized models, database schemas, or
  report-generation behavior as part of this roadmap.
- Do not add external services, tracking, hosted assets, or internet
  dependencies for UX polish.
