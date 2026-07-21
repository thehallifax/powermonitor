# PowerMonitor Beta Readiness Audit

Audit ID: BETA-003
Date: 2026-07-08
Target beta: 5-10 technically minded Western Australian solar/EV households

## Executive summary

PowerMonitor is close to a useful closed beta for technically minded WA
solar/EV users, especially when run in demo mode or by an owner who is already
comfortable with local Python tools, local files, and energy data exports. The
product direction is coherent: it is local-first, financially focused, and
strongest where it explains bills, tariff exposure, EV charging cost, data
confidence, and household decisions.

The current beta risk is not the core product idea. The risk is onboarding and
trust. A new tester can generate demo data, open the local dashboard, upload
Ocular exports, import Synergy bills, and inspect diagnostics, but several
surfaces still assume too much knowledge of the implementation. Setup,
provider management, diagnostics, refresh behaviour, and billing import
accuracy need a pass before inviting testers who are not already close to the
project.

Closed beta should proceed only after the P0 items below are fixed or clearly
disclosed with a safe workaround.

## Current beta readiness verdict

Verdict: not ready for a broad closed beta yet; suitable for an owner-assisted
alpha-beta trial after the must-fix items are addressed.

The project is ready for guided testing by a very small group that understands
local apps and can tolerate rough edges. It is not yet ready for independent
installation by 5-10 external testers because first-run setup, data-source
connection, bill import accuracy, mobile upload reliability, and diagnostic
language can undermine confidence before users reach the useful parts.

Recommended gate: complete a Beta Readiness sprint focused on onboarding,
imports, wording, and diagnostics before issuing invitations.

## Recommended beta audience

Best fit:

- WA households on Synergy plans, especially solar and EV households.
- Technically minded users comfortable with Terminal, local web apps, and
  exporting data files.
- Users willing to provide screenshots, logs, import files with sensitive
  identifiers removed where practical, and clear reproduction steps.
- Users who understand that PowerMonitor is decision support, not billing or
  electrical safety advice.

Avoid for the first beta wave:

- Users who expect a hosted SaaS account or mobile app.
- Users who cannot use local Python tooling without support.
- Users whose only available bill source is a Synergy PDF format not yet
  validated by the importer.
- Users who need non-Synergy tariff support as their primary reason to test.

## What is ready

- Product positioning is clear in `README.md`, `docs/VISION.md`, and
  `docs/UX_ROADMAP.md`: PowerMonitor is a bill explanation, tariff comparison,
  decision, scenario, and ROI modelling platform rather than a Home Assistant
  replacement.
- Demo mode is a strong beta asset. It creates fictional WA household data,
  settings, bills, EV sessions, recommendations, timeline events, health state,
  weather, and a separate demo dashboard under `reports/demo/`.
- The local dashboard has a broad set of testable pages: Home, Glance,
  Recommendations, Bills, Solar, EV, What If, Timeline, Analytics, Settings,
  Setup, Imports, System, and diagnostics.
- The dashboard and README now expose useful period support, including current
  and previous billing periods, calendar periods, year-to-date, last calendar
  year, and custom ranges.
- Provider Management has the right lifecycle verbs for beta needs: configure,
  edit, test, reconnect, disable, enable, remove, and reset-style actions.
- Imports page supports preview-before-commit for Ocular Excel exports and
  Synergy PDF bills, which is the right safety model for beta data.
- The System page is aimed at the correct user question: "Can I trust today's
  advice?"
- Privacy posture is generally strong: local-first dashboard, no CDN assets,
  ignored secrets paths, redacted provider credentials, masked bill identifiers,
  and explicit warnings not to commit cookies, tokens, credentials, account
  numbers, or NMI values.
- Confidence and evidence concepts are present across recommendations,
  decision cards, bill explanations, system trust, data completeness, and
  briefing copy.

## Must fix before closed beta

P0 - Synergy bill import accuracy.
The current fallback PDF extraction can lose layout and still misread totals,
GST, supply charge, and EV Add On buckets. This directly affects financial
trust. Implement the BILL-002 parser hardening pass using anchored section
slicing and validated reconciliation before relying on Synergy PDFs in beta.

P0 - First-run setup path must be independently usable.
README setup is comprehensive but long, and the UI setup path still depends on
the tester understanding where to go next. A beta tester should be able to
start demo mode, configure billing cycle and tariff, connect or skip data
sources, upload files, and understand readiness without being sent into generic
diagnostics.

P0 - Provider onboarding must be reliable from the UI.
A brand-new tester needs to add, edit, reconnect, remove, disable, and test
provider credentials without JSON edits. The architecture has the Provider
Management layer, but the beta gate should verify the complete UI flow for
SolarEdge and Home Assistant-style configuration, including failure messages
and redaction.

P0 - Setup task links must land on the editable thing.
Setup tasks such as tariff, billing cycle, goals, provider connection, imports,
and notifications should deep-link to the exact editable section. Historical
navigation issues have sent users back to Home or broad System pages, which is
too fragile for beta onboarding.

P0 - Mobile upload reliability.
Ocular and Synergy upload flows should be tested on Android before beta.
File inputs must survive dashboard refresh behaviour, and refresh must not
interrupt pending uploads or preview/commit steps.

P0 - Privacy/security beta checklist.
Before inviting testers, provide a short privacy note covering what stays local,
what files may contain sensitive data, which files must not be shared, what is
safe to send for support, and how account numbers/NMI/credentials are masked or
redacted.

## Should fix soon after closed beta starts

P1 - Replace implementation language in user-facing UI.
Several surfaces still expose concepts such as collector, provider runtime,
cookies, refresh schedules, health scores, source readings, raw status, and
class-like names. These should be translated into household language such as
"live data", "data source", "sign-in", "last update", "confidence", and
"technical details".

P1 - Settings/System page structure.
The Settings page now carries setup, imports, provider status, confidence,
privacy-safe diagnostics, live data help, recommendations, runtime details,
provider runtime, source reading diagnostics, household state, behaviour,
anomalies, notification checks, and config summaries. This is powerful but too
large. Closed beta can tolerate it if advanced details remain collapsed, but it
should be split or progressively disclosed soon.

P1 - Daily Briefing wording.
The Daily Briefing should respect current time, adjust morning/afternoon/
evening/night phrasing, avoid internal model names, and make freshness wording
plain and user-friendly.

P1 - Tariff setup and comparison clarity.
Tariff catalogue selection should avoid duplicate EV Add On entries, hide the
manual editor for catalogue tariffs, and present comparisons as outcomes:
"Current bill", "Estimated on comparison plan", and "Estimated savings".

P1 - Empty states for partial data.
When provider observations are absent but historical readings, imported bills,
or EV sessions exist, the UI should explain that existing information still
supports cautious guidance. Empty states should always say what is missing, why
it matters, and what the user can do next.

P1 - Diagnostics support bundle.
A beta tester should know exactly what to provide when reporting a problem:
PowerMonitor version/commit, Python path, command used, page URL/hash, selected
period, import preview error, masked provider status, recent log tail, and
whether demo mode reproduces the issue.

P1 - Android/mobile responsive pass.
Continue testing every page on phone. Several dashboard sections are still
desktop-first, and tables/charts/diagnostics need responsive polish for beta
feedback to be about the product rather than layout strain.

P1 - Ocular EV analytics validation.
Review highest charging power, longest charging session, average duration,
average finish time, average start time, plugged-in versus actual charging
duration, zero-energy sessions, and invalid duration calculations.

P1 - Spreadsheet EV active charging inference.
Spreadsheet EV imports expose connection and disconnection timestamps rather
than active charging timestamps. Infer active charging from the configured
schedule and energy / configured charger power, preserve raw connection values,
and use inferred active intervals for tariff costing and display.

## Known limitations to disclose

- PowerMonitor is local-first and currently requires running Python commands on
  the tester's Mac.
- It is not a hosted service, mobile app, Home Assistant replacement, or smart
  home control platform.
- Demo mode uses deterministic fictional data and should never be mixed with
  real household credentials or bills.
- SolarEdge live data may require credential-based refresh or an advanced
  manual cookie fallback.
- Ocular imports depend on the original charging history export format.
- Synergy PDF bill import is a beta feature and should be treated as
  untrusted until the parser hardening work is complete.
- Non-Synergy and non-WA tariffs are not yet broad catalogue features.
- Recommendations are decision support estimates, not retailer billing advice.
- Forecasts, What If scenarios, tariff comparisons, and EV cost allocation are
  estimates whose confidence depends on data completeness.
- The dashboard is local and offline-capable, but binding the web server to
  `0.0.0.0` makes it reachable on the trusted local network.
- Advanced diagnostics may show local paths, Python environment details, source
  IDs, and timing metadata. They should not expose secrets, but testers should
  review screenshots before sharing.

## Suggested tester onboarding checklist

1. Confirm tester is in WA, uses or understands Synergy tariffs, and has solar
   and/or EV data relevant to the first beta.
2. Explain the product boundary: bill explanation and decision support, not
   automation or official billing.
3. Ask tester to clone or unpack the project locally and create the virtual
   environment.
4. Install optional packages: Playwright for SolarEdge refresh and pypdf for
   PDF fallback.
5. Run demo mode first:
   `python3 collector/report.py --demo` and `python3 collector/web.py --demo`.
6. Open the demo dashboard and check Home, Bills, EV, What If, Glance, Imports,
   and Settings.
7. Configure Home Profile and billing/tariff setup for the real household.
8. Connect only the data sources the tester is comfortable using.
9. Upload an Ocular export or Synergy bill only after explaining preview and
   redaction behaviour.
10. Regenerate the report and review confidence/data quality before acting on
    any recommendation.

## Suggested tester feedback checklist

- What command did you run, and did it complete?
- Which URL or page hash were you viewing?
- Were you in demo mode or real household mode?
- What were you trying to decide?
- What did PowerMonitor make clear?
- What wording felt technical, confusing, or alarming?
- Did any setup task send you to the wrong place?
- Did any page feel too large, too dense, or desktop-only?
- Did import preview match the source file before commit?
- Did any bill, GST, supply charge, tariff bucket, or EV charging number look
  wrong?
- Did PowerMonitor explain confidence clearly enough?
- Did you know what to do next when data was missing?
- What screenshot, masked import preview, log tail, or diagnostic summary can
  you safely share?

## Recommended next development sprint

Recommended sprint: Beta Readiness and Trust Gate.

Mission: make PowerMonitor safe and understandable enough for 5-10 technically
minded WA solar/EV households to install, set up, run demo mode, connect data,
import files, understand confidence, and report problems.

Objective:

- Fix P0 beta blockers in bill import accuracy, setup navigation, provider
  onboarding, mobile upload reliability, and privacy/support guidance.

Scope:

- Harden Synergy PDF parsing and reconciliation.
- Verify first-run setup links and editable sections.
- Exercise provider add/edit/test/reconnect/disable/remove flows.
- Test Android upload behaviour and pending-upload refresh protection.
- Add a concise beta tester guide and support/debug checklist.
- Replace the most visible implementation-language leaks in Home, Setup,
  Imports, Settings, System, and diagnostics.

Validation:

- Run Python compile checks.
- Run unit tests.
- Generate normal and demo reports.
- Manually walk the new tester path on desktop.
- Manually test upload flow on Android or an equivalent mobile browser.
- Run a privacy scan for credentials, cookies, tokens, account numbers, NMI,
  local databases, generated reports, and runtime logs before committing.

Sprint review questions:

1. What changed?
2. Why is the beta experience more trustworthy?
3. What technical or UX debt remains intentionally disclosed?
4. What should the first beta testers focus on?
