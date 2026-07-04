# Changelog

## BILL-001 - Synergy PDF Bill Importer

- Added a Synergy PDF bill importer that extracts invoice metadata, masked
  account/NMI details, billing periods, plan sections, usage/export buckets,
  unit rates, supply charges, GST, and bill totals.
- Added structured bill persistence for Synergy bill headers, plan periods, and
  line items with duplicate invoice protection.
- Extended the Imports flow to preview and commit Synergy PDF bills alongside
  Ocular charging exports.
- Added a Bills & Tariffs reconciliation panel showing the latest actual
  Synergy bill, projected-vs-actual comparison, detected tariff rates, plan
  changes, extraction warnings, and rate mismatches.
- Added regression coverage for Synergy bill parsing, multiple plan periods,
  EV Add On buckets, export credits, supply charges, GST, masking, preview
  before commit, duplicate invoice handling, and projected-vs-actual comparison.

## Bugfix - SolarEdge Zero-Day Diagnostics and Local Day Mapping

- Normalized SolarEdge measurement timestamps to the household's AWST day
  before validation, import, and report summary date filtering.
- Added per-day SolarEdge fetch diagnostics for requested date, API date range,
  measurement range, production/export totals, and all-zero payload detection.
- Prevented zero-only SolarEdge payloads from overwriting existing non-zero
  readings while still allowing later non-zero payloads to repair zero rows.
- Surfaced imported/updated, duplicate, and zero-payload skipped counts through
  collection and reconnect diagnostics.
- Added regression coverage for AWST local-day mapping, zero-payload overwrite
  protection, zero-row repair, and collection payload diagnostics.

## Bugfix - SolarEdge Catch-Up Collection Window

- Changed SolarEdge collection to request at least a four-day catch-up window:
  today plus the previous three days.
- Kept SolarEdge imports idempotent by relying on the importer duplicate check
  and surfacing skipped duplicate counts in collection diagnostics.
- Added collection diagnostics for requested date range, days checked/fetched,
  measurements returned, imported/updated count, and skipped duplicates.
- Passed catch-up diagnostics through SolarEdge reconnect/provider result
  details.
- Added regression coverage for the four-day catch-up window and aggregated
  importer diagnostics.

## Bugfix - SolarEdge Explicit Login Submit Path

- Changed the explicit `login.solaredge.com` path to use the field handles
  returned by successful visible waits instead of re-discovering fields through
  the generic selector path.
- Ensured username and password fill immediately leads to the account submit
  routine, with no generic username-missing fallback after explicit waits
  succeed.
- Added clear failure handling for explicit fields filled but submit not
  attempted versus submitted credentials that remain on the login page.
- Extended safe debug metadata with `explicit_fields_filled` and
  `submit_attempted`.
- Added regression coverage for the current debug scenario where explicit
  waits succeed and submit must still run.

## Bugfix - SolarEdge Account Sign In Button

- Changed `login.solaredge.com` submission to locate the Sign in button
  relative to the password field/form instead of using a global selector.
- Added fallbacks from password form, to nearest password container, to
  password-field Enter, then page keyboard Enter without selecting the
  corporate/SSO Sign in button.
- Added clear failure wording when credentials are submitted but SolarEdge
  remains on the login page.
- Extended safe debug metadata with password form detection, account Sign in
  button detection/click state, Enter fallback usage, and submit URL before
  and after.
- Added regression coverage for account-form submission metadata and
  stuck-login failure handling.

## Bugfix - SolarEdge Login Explicit Wait Path

- Added an explicit `login.solaredge.com` credential-entry path before generic
  login-field discovery.
- Waits up to 30 seconds for visible account email and password fields using
  the live selectors `input[name="username"][type="email"]` and
  `input[name="password"][type="password"]`.
- Fills the account form and clicks the existing-account Sign in button before
  falling back to generic discovery.
- Extended safe debug metadata with explicit path usage, username/password wait
  success, visible field counts, Sign in button count, and clicked selector.
- Added regression coverage proving the explicit path does not require generic
  discovery and still avoids the corporate SSO field.

## Bugfix - SolarEdge Login Form Selectors

- Added `login.solaredge.com`-specific account login selectors for
  `input[name="username"][type="email"]` and
  `input[name="password"][type="password"]`.
- Avoided the corporate/SSO `idpEmail` field when filling saved SolarEdge
  credentials.
- Scoped the Sign in click to the existing account password form so the
  corporate email Sign in button is not selected.
- Added regression coverage for the login.solaredge.com email/password form,
  corporate field avoidance, and existing-account Sign in selection.

## Bugfix - SolarEdge Log In Candidate Click

- Removed the requirement for `div#se-signing-mfe` to exist before attempting
  the initial SolarEdge `Log in` button click.
- Changed initial login handling to iterate all candidate selectors and click
  the first visible, enabled candidate.
- Added `text="Log in"` and `role=button[name=/log in/i]` candidate selectors.
- Extended safe debug metadata with selector attempts, candidate counts,
  visible counts, clicked selector, and skip reasons when candidates are found
  but none can be clicked.
- Added regression coverage for candidate-found-but-not-clicked diagnostics.

## Bugfix - SolarEdge Initial Log In Button Selector

- Made SolarEdge session refresh wait for `div#se-signing-mfe` before looking
  for the initial landing-page `Log in` control.
- Added explicit selectors for `#se-signing-mfe button:has-text("Log in")`,
  `button:has-text("Log in")`, and `button[type="button"]:has-text("Log in")`.
- Added post-click waiting for navigation or username/password fields while
  preserving the credential field retry loop.
- Extended safe debug metadata with `#se-signing-mfe` detection and candidate
  Log in button counts.
- Added regression coverage for the live SolarEdge auth shell/button shape.

## Bugfix - SolarEdge Initial Log In Button

- Changed SolarEdge session refresh to start from the current
  `/mfe/auth/` entry point.
- Added first-page `Log in` button detection/clicking before credential field
  discovery when the username and password fields are not visible yet.
- Added retry-based credential field discovery after the first login click so
  dynamic redirects/forms can load before failing.
- Extended safe debug metadata with initial login button found/clicked state,
  URL before and after the click, and field search attempts without logging
  credentials, cookies, tokens, headers, or HTML.
- Added regression coverage for the initial login-button flow.

## Bugfix - SolarEdge Post-Login Site Navigation

- Updated SolarEdge session refresh to continue past login into the configured
  site dashboard before accepting cookies or verifying data access.
- Added Sites-list detection, configured site-id dashboard navigation, and a
  first-site fallback click when direct site navigation is unavailable.
- Required dashboard proof via route or dashboard indicators before calling the
  SolarEdge data endpoint.
- Added safe navigation debug metadata covering title, hostname, route, Sites
  list detection, site-row click state, dashboard-loaded state, and configured
  site-id usage without logging secrets or page HTML.
- Added regression tests for Sites-list detection, configured-site navigation,
  first-site fallback, dashboard-before-data verification, and no-site failure.

## Bugfix - SolarEdge Playwright Login Selectors

- Made the SolarEdge session refresh automation handle current and fallback
  login-field selectors for email/username and password fields.
- Added support for two-step SolarEdge login flows with Continue/Next before
  the password field appears.
- Added safe login-failure diagnostics with page title, hostname, field-found
  booleans, challenge/SSO likelihood, and redacted debug metadata without
  dumping page HTML or credentials.
- Improved login-field failure messages so reconnect/test failures explain the
  page state instead of only saying fields were missing.
- Added selector fallback regression tests using a fake Playwright page.

## Bugfix - SolarEdge Reconnect Data Collection

- Unified SolarEdge Test Connection, Reconnect, and scheduled collection around
  the same session recovery and data retrieval helper.
- Changed Reconnect to use saved-credential recovery through the collection
  path instead of forcing the older refresh-only path before import.
- Made successful reconnects report real measurement/import counts, auth path,
  and dashboard regeneration status.
- Added stage-specific reconnect failure wording for missing credentials, login
  failure, session refresh failure, data retrieval failure, import failure, and
  report regeneration failure.
- Added safe SolarEdge provider-action logging for auth path, fetch/import
  attempts, report regeneration, and result without logging credentials or
  session values.
- Added regression coverage for shared recovery, scheduled collection import,
  reconnect import reporting, legacy-refresh avoidance, and stage-specific
  failures.

## LD-001 - Live Data Lifecycle

- Tightened SolarEdge Test Connection so it verifies usable recent solar data,
  not just authentication.
- Added a clear "Signed in, but no recent solar data was received" warning
  when credentials work but SolarEdge returns no usable readings.
- Made SolarEdge Reconnect run the refresh/import/report path so successful
  reconnects update readings and regenerate the dashboard.
- Updated provider lifecycle recording so SolarEdge only becomes Connected
  after data retrieval succeeds.
- Reworked live provider cards to show homeowner-facing status, last
  successful refresh, next refresh, data available, what PowerMonitor receives,
  and action needed, with runtime/sync/lifecycle details kept in Advanced
  Diagnostics.
- Hid connected-provider credential fields behind Edit credentials and removed
  the Connect button once a live provider is connected.

## Bugfix - Provider Action UX and SolarEdge Test Flow

- Changed browser-submitted provider actions to redirect back to the System
  provider card with a visible status banner instead of displaying raw JSON.
- Kept JSON responses for API/XHR provider action callers.
- Added server-rendered provider action feedback for Save, Connect, Test
  Connection, Disconnect, and provider action failures without exposing
  credentials in URLs, logs, HTML, or status text.
- Confirmed SolarEdge Test Connection refreshes the session through the saved
  credential path before retrying the SolarEdge request.
- Tightened provider card spacing so live provider forms sit cleanly inside
  the redesigned data-source cards on desktop and mobile.

## Bugfix - Provider Connect Form Submission

- Made System / Your Data Sources provider forms standalone POST forms so
  SolarEdge Connect submits to `/api/providers/solaredge/save` instead of
  falling through to the billing-cycle setup endpoint.
- Added provider action form metadata for provider id, action type, and
  credential fields while preserving secret redaction.
- Added provider action POST logging for provider id, action, and result
  status without logging submitted credential values.
- Added regression coverage for billing/provider form separation, SolarEdge
  provider endpoints, credential fields, and provider action logging.

## TRUST-002 - SolarEdge Credential Recovery

- Made SolarEdge collection recover from missing or expired cookies by
  attempting saved-credential sign-in, refreshing the local session cookie, and
  retrying the failed collection once.
- Changed SolarEdge setup to prefer username/email and password while keeping
  cookie entry as an advanced manual fallback.
- Added clearer collector and provider statuses for missing credentials,
  sign-in failures, expired cookies, and SolarEdge/network unavailability.
- Updated Home/System recovery wording so stale or failed SolarEdge auth no
  longer appears healthy and credentials/secrets stay redacted.
- Added regression tests for automatic recovery orchestration, cookie saving,
  missing-credential guidance, failed-login provider state, and secret
  redaction.

## FT-001 - First-Time Experience

- Reworked Setup into a seven-question first-time flow covering location,
  retailer, electricity plan, billing cycle, solar, EV charging, and battery.
- Added local, non-secret setup choices for optional solar, EV, and battery
  services so skipped services still feel usable and can be connected later.
- Added a finish summary showing what PowerMonitor can explain now, what is
  optional, and the fastest path to a useful first recommendation.
- Updated Setup and Home partial-state wording to avoid architecture terms and
  use homeowner language such as connected services, imported history,
  electricity plan, and advice confidence.
- Added setup-flow, web-config, and mobile presentation regression coverage.

## TRD-001 - Trends & Records

- Added a reusable Trends Engine that compares current household history with
  the previous comparison period to produce long-term trend context rather than
  recommendations or insights.
- Added personal household records for daily history, including solar
  generation, lowest cost, export, self-consumption, EV charging, and home
  energy-use records with previous-record context where available.
- Added a concise Home "Trends and records" section limited to two trends and
  one record, with no graphs and homeowner-friendly wording.
- Exposed trends and records in summary/report data as `household_trends`.
- Added regression tests for trend direction, record detection, duplicate-topic
  suppression, insufficient history, and Home display limits.

## INS-001 - Household Insights

- Added a Household Insights observation engine that surfaces interesting
  household changes without creating recommendations or action items.
- Added a Home dashboard section for up to three plain-language insights, each
  showing what changed, the comparison period, why it is interesting,
  confidence, and supporting values.
- Generated insights from existing household summaries and daily trends, such
  as solar changes, export/self-consumption shifts, low-cost streaks, standout
  solar or cost days, EV cheap-tariff charging, bill projection movement, and
  lower peak-period cost.
- Exposed household insights in summary data for inspection while keeping the
  engine source-agnostic and free of database access.
- Added regression coverage for insight generation, quiet ordinary-history
  behaviour, measured EV tariff observations, and the three-insight Home limit.

## FIN-002 - Bill Explainability

- Added Financial Engine bill-change contribution analysis comparing the
  current projection with the previous completed billing period.
- Added homeowner-friendly "Why has my bill changed?" sections on Home and
  Billing pages with ranked contributors, positive changes, negative changes,
  confidence wording, and uncertainty notes.
- Added EV-specific bill explanation when actual charger data exists, including
  EV energy, charging cost, all-Peak comparison, off-peak savings, and optional
  petrol-equivalent cost from configured vehicle/fuel assumptions.
- Exposed bill-change explanation data in the dashboard API payload.
- Added tests for contribution ranking, narrative generation, missing-history
  handling, and EV-specific explanation output.

## UX-005 - Trust & Confidence Experience

- Renamed the primary trust view to "Can I trust today's advice?" and made it start with a homeowner confidence summary, available information, missing information, and the effect on advice.
- Replaced "Source readings" with "Recent household updates" using friendly provider names, descriptions, and relative dates while keeping raw timestamps in Advanced Diagnostics.
- Reworked Setup review actions so live-data, advice-trust, and Energy Score links land directly on pages that answer the implied homeowner question.
- Removed the Recommendation Centre setup task and moved Energy Score review into advice readiness.
- Added a direct Energy Score explanation anchor with cost, solar, peak usage, overnight usage, and household behaviour breakdowns.

## FIN-001 Follow-up - Tariff Setup Display Fixes

- Hid manual tariff input fields for catalogue tariff selections and show them
  only when Manual tariff is selected.
- Added read-only catalogue tariff previews for setup, including supply charge,
  export credit, import rates, and tariff window notes.
- Removed the optional add-on dropdown from setup so Synergy EV Add On appears
  only as a plan option.
- Differentiated catalogue rates and window descriptions for Synergy Home Plan,
  Synergy EV Add On, and Previous Flat/Home Plan.
- Added regression coverage for manual-field visibility, catalogue previews,
  plan/add-on de-duplication, and catalogue rate/window differences.

## FIN-001 - Australia Tariff Setup

- Added a static Australia-first tariff catalog for WA/Synergy setup, including
  Synergy Home Plan, Synergy EV Add On, and Previous Flat/Home Plan.
- Replaced free-text retailer setup with structured state/territory, retailer,
  plan, and billing cycle start-day fields.
- Added Manual tariff fallback with explicit import, supply, and export rate
  entry for unsupported retailers or plans.
- Wired tariff selection through saved settings so billing, recommendations,
  and setup readiness use the selected catalog/manual rates.
- Added tests for WA/Synergy plan selection, manual
  tariff fallback, and setup readiness.

## SY-001 - Product Coherence: Data Sources

- Reframed the System page around homeowner questions: what information
  PowerMonitor has, whether today's advice can be trusted, and whether any
  setup/source action is needed.
- Replaced primary provider presentation with "Your Data Sources" cards that
  represent recognizable household sources such as SolarEdge, Ocular, Synergy,
  and Home Assistant rather than internal connector concepts.
- Added source-type presentation rules so live services show connection
  actions, imported data shows upload actions, and household configuration
  shows edit-configuration actions.
- Added a "Today's advice uses" trust checklist and a Setup "Available
  integrations" grouping for connected versus optional integrations.
- Hid non-user-facing concepts such as smart meter readings, provider registry,
  household state, and runtime diagnostics from the primary System experience,
  while leaving them available under Advanced Diagnostics.

## BF-002 - Household Trust

- Tightened Provider Management so `Connected` is only shown after configuration
  exists, a connection test succeeds, and provider data has been retrieved.
- Reworded provider cards around homeowner trust: status, last successful
  update, what the provider contributes, and action required, with technical
  runtime details behind Advanced Diagnostics.
- Changed Glance Mode so stale live data uses recent household information
  instead of showing a blanket unavailable state unless no usable household data
  exists.
- Reframed the System page around "Can I trust today's advice?" with plain
  source and limitation summaries.
- Added regression coverage for stricter connected-state validation, historical
  import status, Glance fallback wording, and simplified provider cards.

## BF-001 - Household Intelligence

- Added homeowner-facing recommendation rationale fields so each decision-driven
  recommendation explains what happened, why PowerMonitor thinks it happened,
  what to check, and what may happen if ignored.
- Reworked insufficient-data recommendations to say PowerMonitor does not yet
  have enough information, instead of presenting generic low-confidence action
  cards.
- Suppressed generic `NO_ACTION` decisions from recommendation cards while
  keeping them available as platform decisions.
- Tightened Home so it shows at most one primary recommendation and two
  secondary observations.
- Added household-specific baseload and billing rationale using existing
  Household Behaviour, Household State, Context, and Financial metadata without
  adding new calculations.
- Added regression tests for recommendation rationale, insufficient data,
  baseload explanation, recommendation mapping, and Home card limits.

## UX-004 - Home Experience

- Redesigned Home around a homeowner-assistant flow: what to do, charging
  timing, spending status, data health, good signs, and what changed.
- Replaced recommendation/advisor counts with meaningful household summaries
  such as "Everything looks normal" and "We found one thing worth checking."
- Merged Best Action and Watch items into one deduplicated "What should I do?"
  section, and moved positive signals into encouraging "Good signs" copy.
- Moved setup progress below the Home guidance so first-time users see the
  current household answer before configuration detail.
- Added Home presentation regression coverage for Glance summary, duplicate
  card removal, and count-free assistant copy.

## UX-003 - Decision Wording and Trust

- Reworded Glance unavailable and peak-tariff states so missing live data no
  longer sounds like a charging warning.
- Softened recommendation, briefing, provider, and system copy around low
  confidence, missing readings, expired live-data sign-in, and potential
  issues.
- Updated anomaly presentation wording to "Potential issue detected" and
  "Review the supporting data before taking action."
- Added regression coverage for unavailable live data, low-confidence wording,
  time-aware briefing copy, and anomaly presentation labels.

## UX-002 - Navigation and Action Links

- Retargeted setup actions so tariff settings, live data review, goals review,
  import history, billing cycle, and data quality links open the specific
  dashboard section needed to act.
- Added deep hash routing support for nested Setup and System anchors so links
  reveal the parent page, open containing disclosure panels, scroll to the
  target, and highlight provider cards.
- Expanded provider cards with configuration controls visible by default and
  added a clear Glance return-to-dashboard action.
- Added regression coverage for major action targets and presentation anchors.

## UX-001 - Human Language Pass

- Reworded customer-facing Home, Daily Briefing, Glance, Recommendations,
  Setup, Imports, Timeline, and System presentation copy to avoid exposing
  internal engine, model, runtime, collector, and observation terminology.
- Added report display helpers that translate internal source and evidence
  labels into homeowner-friendly language while preserving existing APIs,
  models, and architecture.
- Updated recommendation, notification, setup, timeline, and confidence wording
  so unavailable savings read as "Unable to estimate" and data health reads as
  live data/source confidence.
- Updated wording regression expectations and verified the generated report no
  longer contains the targeted customer-facing engineering terms.

## PI-006 - Interactive Provider Configuration

- Added local interactive provider configuration controls to System provider
  cards for save, test connection, reconnect, reset, disable, enable, and
  remove actions.
- Added normalized provider action results with success/warning/failure status,
  plain-language messages, next steps, and technical details behind disclosure.
- Added safe local provider action endpoints that redact sensitive values and
  avoid logging submitted secrets.
- Added local Provider Manager state persistence for enabled/disabled/removed
  providers and configured-field metadata.
- Wired Home Assistant UI mapping fields into Provider Manager save/reset
  actions so entity mappings can be manually edited, validated, saved, and
  reset from the local web UI.
- Wired Home Assistant endpoint/token and SolarEdge session details into
  existing local settings/secret surfaces where practical, with sensitive
  fields remaining write-only in reports and API responses.
- Clarified Ocular export as an import-provider workflow instead of requesting
  account credentials.
- Added tests for provider save/test/reset/disable/enable/remove actions,
  Home Assistant mapping save, redaction, invalid configuration validation,
  Ocular import-provider configuration, and web payload normalization.

## PI-005 - Home Assistant Entity Configuration

- Added a reusable provider entity mapping model with Home Assistant entity
  categories for Grid, Solar, Home Consumption, Battery, EV Charger, EV Status,
  Energy, and Other.
- Added Home Assistant mapping recommendations, manual selected mappings,
  reset-to-recommended support, duplicate assignment warnings, availability
  checks, and compatible-unit validation.
- Updated the Home Assistant adapter to use selected mappings when collecting
  observations while preserving automatic discovery when no mappings are saved.
- Persisted Home Assistant entity mappings through Provider Manager using local
  non-secret provider mapping configuration that survives reconnects and runtime
  restarts.
- Expanded provider diagnostics and health payloads with selected entity,
  entity state, last update time, unit, availability, validation status, and
  last successful observation where available.
- Added System provider card disclosure for entity mapping guidance while
  keeping raw technical details behind the existing diagnostics disclosure.
- Added regression tests for entity grouping, recommendations, manual override,
  invalid-unit warnings, mapping persistence, and connection-test diagnostics.

## PI-004 - Live Power and Battery Observations

- Added first-class provider observation types for live power, grid live power,
  solar live power, battery state/SOC/power, EV charger live power, EV
  connected/charging state, and home live power.
- Added live observation promotion validation for valid units, battery SOC
  bounds, stale live readings, impossible negatives, and signed grid/battery
  power where direction is meaningful.
- Updated Home Assistant entity mapping to emit normalized live observations
  instead of forcing current-state values through historical energy types.
- Mapped accepted live observations into Household State for current grid
  import/export, solar production, battery SOC and charge/discharge, EV
  connected/charging state, and EV charging power.
- Updated Context Engine and Charging Advisor evidence so live solar surplus,
  grid export/import, and active EV charging can influence Glance/advisor
  context when live provider evidence is available.
- Exposed live power and battery values on the System Household State card and
  provider diagnostics via existing serialization paths.
- Added regression tests for live observation validation, battery SOC
  validation, signed power handling, Home Assistant live mapping, Household
  State live mapping, Context live evidence, and API-safe serialization.

## PI-003 - Home Assistant Integration

- Added a Home Assistant Provider Runtime adapter using the existing provider
  contract with URL plus long-lived access token authentication.
- Added Home Assistant connection testing with authentication validation,
  version detection, entity count, discovered datasets, plain-language failures,
  and technical diagnostics through the standard Provider Test Result model.
- Added Home Assistant entity discovery for power sensors, energy sensors,
  solar production, battery sensors, and EV charging entities.
- Mapped supported Home Assistant entities into normalized Provider
  Observations so runtime collection flows through the Observation Pipeline and
  Promotion Engine path.
- Registered Home Assistant in the Provider Framework and wired it into the
  existing Provider Manager and Provider Runtime views alongside SolarEdge with
  no provider-specific UI.
- Added regression tests for Home Assistant capability discovery, observation
  mapping, connection testing, runtime persistence, and registry exposure.

## PI-002 - SolarEdge Production Integration

- Promoted SolarEdge as the reference provider workflow by wiring Provider
  Management to SolarEdge runtime status and adapter-backed connection testing.
- Added normalized connection-test results with success/warning/failure status,
  plain-language messages, next steps, and technical details for SolarEdge
  authentication failures.
- Expanded provider onboarding with runtime state, collector duration,
  consecutive failures, retry countdown, health score, and health factors for
  authentication, freshness, runtime success, observation quality, and collector
  health.
- Tightened provider configuration readiness so required SolarEdge cookie data
  must be present before configuration is treated as ready.
- Improved SolarEdge import output with data received, imported time range,
  records imported, duplicates ignored, and observations promoted.
- Added regression tests for SolarEdge connection guidance, runtime-backed
  onboarding health, required cookie readiness, and SolarEdge import summaries.

## PI-001 - Provider Onboarding Experience

- Added a standardized provider onboarding contract covering not configured,
  configuration required, ready to connect, connecting, connected, syncing,
  warning, error, and disabled states.
- Expanded managed provider payloads with plain-language setup guidance:
  required information, why it is required, connection status, health status,
  data availability, sync frequency, last successful sync, next scheduled sync,
  common failures, recovery steps, and technical details.
- Reworked System provider cards so first-time users see simple connection,
  health, sync, data, and next-action answers before expandable setup/recovery
  and technical diagnostics.
- Linked import provider rows directly to the matching System provider setup
  card so Setup and Imports workflows land on relevant configuration details.
- Added regression coverage for provider onboarding serialization, standardized
  states, mobile provider cards, and provider setup deep links.

## BR-003 - Guided Onboarding

- Added reusable disclosure panels so dense dashboard, financial, import, and
  system details can stay available without overwhelming first-time users.
- Refocused Bills around a financial overview, headline bill cards, savings
  opportunity, and collapsed detailed tariff calculations/charts.
- Added guided Setup and Imports onboarding steps that explain the basic path,
  supported Ocular Excel exports, preview behavior, import outcomes, and common
  troubleshooting.
- Reworked System into progressive sections for Household Status, Providers,
  Collector Health, Decisions, Evidence, and Advanced Diagnostics.
- Updated provider cards to answer whether each provider is connected, healthy,
  recently updated, and what the user should do next.
- Added regression tests for disclosure panels, import guidance, and provider
  onboarding cards.

## BR-002 - Product Experience

- Added consistent Home breadcrumbs to primary dashboard pages so users always
  have a clear route back to the main view.
- Tightened Setup task destinations: tariff and billing tasks open the billing
  configuration panel, EV history opens Imports, and collector/data-quality
  tasks jump to the matching System diagnostics.
- Removed the unfinished notification setup item from active setup rendering so
  users are not sent to a future placeholder workflow.
- Improved Recommendation Centre cards with clearer action emphasis,
  conversational confidence notes, and more approachable empty-state wording.
- Grouped Timeline events by day, added compact event markers, and replaced raw
  event type strings with readable labels.
- Improved financial and provider-management presentation with clearer bill
  attribution guidance, friendlier comparison labels, provider empty states,
  connection-health wording, and user-facing form/upload validation messages.
- Added regression tests for breadcrumbs, timeline grouping, setup destinations,
  and revised upload errors.

## BR-001 - Mobile Experience

- Added a compact mobile shortcut rail for Home, Glance, Setup, and System so
  phone users always have a clear path back to primary dashboard areas.
- Refined responsive dashboard CSS for narrow screens, including stacked cards,
  safer typography, mobile-friendly controls, contained tables, touch-friendly
  upload inputs, and reduced body-level horizontal overflow.
- Improved Imports copy and file-picker metadata for phone/tablet uploads, with
  explicit messaging that dashboard pages will not auto-refresh while files are
  being selected or uploaded.
- Moved kiosk auto-refresh to the dedicated `/glance` local web route so Glance
  remains display-friendly without risking upload state loss on the main
  dashboard.
- Added regression tests for mobile navigation, upload copy, dashboard refresh
  behavior, and Glance kiosk refresh behavior.

## PB-026 - Legacy Recommendation Retirement

- Retired EV session analytics recommendation generation from report/API output;
  EV session analytics now remains evidence-only.
- Disabled legacy advisor-derived notification generation by default and kept it
  available only through an explicit compatibility flag.
- Hardened advisor compatibility wrappers so attached advisor cards use
  Decision Pipeline action text, while unmatched advisors are marked
  evidence-only.
- Updated Goals Engine output so goal progress no longer reuses advisor-native
  action wording.
- Marked the old `RecommendationEngine`, EV-session recommendation helpers, and
  legacy decision wrappers as deprecated compatibility surfaces.
- Added regression tests proving API recommendations are decision-native,
  legacy advisor fallback is disabled by default, advisor wrappers cannot
  create independent actions, goals do not emit advisor-native actions, and
  retained legacy paths require explicit opt-in.

## PB-025 - Decision Pipeline Cleanup

- Added a canonical `DecisionPipeline` facade for the platform-owned flow from
  `HouseholdDecision` to `RecommendationPresentation`.
- Updated report/API assembly to build recommendation presentations once from
  the Decision Pipeline and reuse them for Daily Briefing, Glance Mode,
  Recommendation Centre, and notification readiness.
- Refactored Daily Briefing to consume `RecommendationPresentation` directly;
  Household Decision input now remains as a compatibility wrapper that adapts
  through `RecommendationAdapter`.
- Added notification generation from `RecommendationPresentation` and gated
  advisor-derived notification generation behind an explicit legacy
  compatibility flag.
- Extended `RecommendationPresentation` lineage with explicit context and
  behaviour payload links in addition to Household Decision, advisor
  explanation, Household State, State Change IDs, and evidence.
- Added PB-025 regression tests covering end-to-end pipeline output, briefing,
  glance, notifications, recommendation ordering, and legacy fallback control.

## PB-024 - Decision Recommendation Adapter

- Added a Decision Recommendation Adapter that converts Household Decisions into
  presentation-ready recommendation contracts with urgency, category, icon,
  evidence, rationale, confidence, and priority metadata.
- Added shared recommendation presentation models for adapter output, evidence,
  reasons, priorities, and categories.
- Refactored advisor presentation, Daily Briefing, Glance Mode, Recommendation
  Centre, Solar, What-if, and API recommendation output to consume
  `RecommendationPresentation` objects where recommendations are shown.
- Kept legacy recommendation compatibility behind thin presentation wrappers so
  existing advisor/report paths continue to behave while visible
  recommendations originate from Household Decisions.
- Added adapter and API serialization regression tests for charging, billing,
  monitoring, anomaly investigation, no-action, priority ordering, and
  recommendation lineage.

## PB-023 - Advisor Decision Migration

- Added advisor presenter support that turns platform-owned Household Decisions
  into advisor explanation cards with decision references, evidence, confidence,
  and user-facing wording.
- Refactored Daily Briefing to summarize Household Decisions when provided,
  while retaining advisor-based briefing compatibility for existing callers.
- Added Household Decision provenance to Recommendation Centre cards, API
  recommendation serialization, advisor explanations, and System debugging.
- Extended timeline entries so decision-backed events can reference
  `decision_id` and `decision_type` where appropriate.
- Updated Notification Readiness to prefer Household Decisions over advisor
  outputs when decision data is available.
- Added migration tests for advisor explanation, briefing, recommendation
  mapping, notification mapping, timeline integration, and serialization.

## PB-022 - Decision Engine

- Added normalized `DecisionEngine` household decision contracts for decision
  type, priority, confidence, evidence, reasons, outcomes, status, capability
  requirements, financial metadata, and serialization.
- Added DataEngine-backed household decision generation from Household State,
  Household Context, Behaviour, Anomalies, capabilities, provider health,
  financial metadata, and State Changes.
- Exposed `household_decisions` and `decision_summary` through dashboard/API
  output without replacing existing recommendation cards.
- Updated Notification Readiness to consume normalized Household Decisions in
  addition to state changes and advisors.
- Added a System page Decision Summary card showing active decisions,
  confidence, priority, reasons, and supporting evidence.
- Added Decision Engine tests for charging, insufficient evidence, high-cost
  waiting, anomaly investigation, capability-aware setup, serialization, and
  notification consumption.

## PB-021 - Behaviour-Aware Anomaly Engine

- Added `AnomalyEngine` with household-relative anomaly models for baseload,
  EV charging, solar behaviour, billing, collector/provider health, evidence,
  severity, and confidence.
- Added DataEngine-backed anomaly generation from current Household State,
  Household Context, recent State Changes, learned Household Behaviour, and
  current observation summaries.
- Exposed `household_anomalies` through dashboard/API output for future advisor
  and notification consumption without changing advisor recommendations.
- Added a System page Household Anomalies card showing active anomaly title,
  severity, confidence, first detected timestamp, and evidence summary.
- Added anomaly tests for normal behaviour, overnight load, EV charging, solar,
  billing, collector/evidence degradation, confidence, and serialization.

## PB-020 - Household Behaviour Engine

- Added `BehaviourEngine` with descriptive household behaviour profiles for
  baseload, daily usage, EV charging behaviour, solar usage, and confidence.
- Added DataEngine-backed behaviour generation from normalized historical grid
  intervals, solar intervals, and EV sessions without querying SQLite outside
  DataEngine.
- Exposed `household_behaviour` through dashboard/API output.
- Added a System page Household Behaviour card showing overnight baseload,
  average wake time, typical EV charging behaviour, solar export tendency, and
  confidence.
- Added behaviour tests for weekday/weekend learning, sparse data confidence,
  serialization, household isolation, and API output.

## PB-019 - Notification Preferences and History

- Added household-scoped notification preferences for global enablement,
  per-type rules, priority thresholds, quiet hours, channel preference
  placeholders, and cooldown periods.
- Added notification history persistence for candidate keys, delivery state,
  sent/suppressed timestamps, suppression reasons, priority, expiry, and
  evidence summaries.
- Updated `NotificationEngine` to suppress candidates based on preferences,
  quiet hours, disabled types, priority thresholds, expired candidates, and
  recently sent duplicate history.
- Exposed `notification_preferences`, `notification_history`, and
  preference-filtered `notification_readiness_summary` through dashboard/API
  output.
- Added System notification preference/history cards and an optional Setup
  notice when notifications are disabled.

## PB-018 - Notification Readiness

- Added `NotificationEngine` with typed notification candidates, priorities,
  channels, evidence, readiness summaries, expiry, and suppression reasons.
- Converted state changes, advisors, setup readiness, household context, and
  evidence quality into local-only notification candidates without sending any
  external alerts.
- Added suppression rules for weak evidence, duplicate events, expired
  recommendations, irrelevant context, and incomplete setup.
- Exposed `notification_candidates` and `notification_readiness_summary`
  through dashboard/API output.
- Added a System page Notification Readiness summary card and tests for
  candidate generation, suppression, duplicate handling, expiry, household
  scoping, and serialization.

## PB-017 - State Change Detection

- Added `StateChangeEngine` to compare persisted Household State snapshots and
  emit meaningful tariff, solar, power direction, collector, billing, evidence,
  and provider change events.
- Added household-scoped `state_changes` persistence in DataEngine with
  duplicate suppression, evidence, severity, timestamps, and recent/summary
  query APIs.
- Exposed `state_change_summary` and `recent_state_changes` through
  dashboard/API output.
- Added recent state changes to the System page and included meaningful state
  changes in the Decision Timeline.
- Added state change detection, duplicate suppression, household isolation, and
  API serialization tests.

## PB-016 - Household State Snapshots

- Added household-scoped `household_state_snapshots` persistence in DataEngine
  with tariff, power, energy, solar, EV, battery, billing, collector, evidence,
  provider provenance, context summary, and full state JSON columns.
- Added material-hash duplicate suppression so repeated report generations do
  not create excessive snapshots when household state has not changed.
- Exposed `latest_household_state_snapshot`,
  `recent_household_state_snapshots`, and the current
  `household_state_snapshot` through dashboard/API output.
- Updated the System Household State section with latest snapshot timestamp and
  recent material state changes.
- Added snapshot persistence, duplicate suppression, household isolation,
  redaction, and API serialization tests.

## PB-015 - Household State Engine

- Added `HouseholdStateEngine` with canonical household state models for power,
  energy, tariff, EV, solar, battery, billing, collector, and evidence facts.
- Added serialized `household_state` to dashboard/API output and a System page
  Household State section for current tariff, import/export, solar, EV,
  battery, billing, collector, and evidence confidence.
- Updated Context Engine to consume Household State when available instead of
  reconstructing current conditions directly from raw summaries.
- Threaded provider management, provider runtime, provider observations, and
  promotion summaries into Household State without querying providers directly.
- Added Household State tests for creation, tariff transitions, billing
  transitions, provider/collector health changes, evidence degradation, context
  integration, and API serialization.

## PB-014 - Provider Adapter Runtime

- Added `ProviderRuntime` with standard adapter execution, schedules, retry
  handling, exponential backoff, authentication refresh, metrics, runtime
  health, and observation submission.
- Added the shared `ProviderAdapter` contract and execution models for
  authentication, schedules, execution results, metrics, and runtime health.
- Added `SolarEdgeAdapter` as the reference adapter around the existing
  SolarEdge collector while preserving the legacy `collect_today.py` entry
  point.
- Exposed Provider Runtime status through dashboard JSON and local
  `/api/runtime`, `/api/runtime/providers`, and `/api/runtime/provider/<id>`
  endpoints.
- Added a System page Provider Runtime card and runtime tests covering
  execution, scheduling, retries, authentication refresh, health, and
  observation submission.

## PB-013 - Provider Management

- Added `ProviderManager` with configuration, credential, lifecycle, health,
  sync, diagnostics, capability, and connection-test models above the existing
  Provider Framework.
- Exposed sanitized provider management data through dashboard JSON plus local
  `/api/providers`, provider detail, test, configure, and diagnostics routes.
- Replaced the System page's static provider registry card with model-driven
  Provider Management cards and linked Setup provider tasks directly to those
  cards.
- Updated Imports to show provider-managed import providers and the provider
  attached to recent observations.
- Added provider management tests covering lifecycle, credential redaction,
  state transitions, diagnostics, capability detection, connection testing,
  removal/reconnect actions, and future provider configuration.

## PB-012 - Beta Readiness & UX Polish

- Added a Billing Cycle setup section with local editing for billing start
  day, optional retailer, and supported tariff plan.
- Added a local `/setup/billing-cycle` settings update route that completes the
  related Setup task without storing secrets.
- Extended visible period selection with previous billing period, last month,
  year to date, and last calendar year.
- Corrected EV analytics by using circular average clock times, suppressing
  misleading derived highest-power values, and adding average session duration.
- Updated timeline billing-period wording and persistence so created/updated
  billing-period events do not duplicate.
- Improved Collector Health, Provider Observation, Charging Advisor, and
  Glance wording for beta clarity and context-aware relevance.

## PB-011 - Advisor Context Integration

- Threaded shared `HouseholdContext` into Cost, Solar, Efficiency, and Briefing
  engines so advisor wording uses one current household state.
- Added night-aware Solar Advisor wording that reviews today's performance
  without implying immediate solar action when context reports
  `SOLAR_IMPOSSIBLE`.
- Added Cost Advisor context evidence for billing stage and tariff state, and
  Efficiency Advisor context evidence for time and evidence freshness.
- Updated Daily Briefing wording to frame night, morning, daylight, and evening
  priorities from shared household context.
- Added advisor and briefing regression tests for night/day context behaviour
  and serialized context references.

## PB-010 - Context Engine

- Added `ContextEngine` with descriptive household context models for time,
  solar, tariff, charging, billing, evidence, and confidence.
- Centralized current tariff-window and charging-context classification for
  Charging Advisor while preserving existing advice outcomes.
- Exposed serialized household `context` through the dashboard API and Glance
  state for future briefing and notification surfaces.
- Added a System page Context Summary card showing inferred household state,
  confidence, and data sources used.
- Added Context Engine tests for daytime, nighttime, tariff transitions,
  billing-period transitions, evidence degradation, serialization, and advisor
  context output.

## PB-009 - Observation Promotion Engine

- Added `PromotionEngine` with promotion candidates, decisions, reasons,
  validation, conflicts, results, and explicit promotion states.
- Added validation, deduplication, supersession, conflict, freshness,
  household-scope, and capability-compatibility checks for provider
  observations before promotion.
- Added household-scoped `promotion_decisions` persistence in DataEngine as a
  trusted read-model layer without replacing existing domain tables or
  calculations.
- Exposed `promotion_summary`, `conflict_summary`, and `latest_promotions`
  through the dashboard API and System page.
- Added Setup-page guidance when repeated provider conflicts are detected.
- Added promotion, conflict, duplicate, household isolation, persistence, and
  API serialization tests.

## Housekeeping - Charging Tariff Windows

- Extended `ChargingAdvice` with current, next-lower-cost, lowest-cost, and
  recommended-wait tariff window fields while preserving existing compatibility
  aliases.
- Updated Charging Advisor tariff wording to distinguish the next cheaper
  window from the lowest-cost window.
- Changed Glance and Home wording to show "Next cheaper window" and
  "Lowest-cost window" separately.
- Added regression coverage for multi-step tariff structures where a nearer
  cheaper window exists before the lowest-cost window.

## PB-008 - Provider Observation Pipeline

- Extended provider observations with household, provider, capability,
  observation type, timestamps, value, unit, freshness, quality, provenance,
  raw reference, and import-job linkage fields.
- Added household-scoped `provider_observations` persistence plus DataEngine
  write/read APIs and an API-safe provider observation summary.
- Linked Ocular EV imports to normalized `ev_session` provider observations
  without replacing existing EV session domain tables or calculations.
- Added provider observation metadata to capability reasons, System page latest
  observations, Imports history observation counts, and dashboard API output.
- Added persistence, household isolation, API serialization, and
  importer-to-observation regression tests.

## PB-007 - Provider Framework

- Added shared provider framework models for providers, provider types,
  capabilities, statuses, freshness, normalized observations, observation
  quality, and registry serialization.
- Registered current conceptual providers for SolarEdge, smart meter readings,
  Ocular export, Synergy tariff/config, and Home Assistant OCPP when configured.
- Exposed `provider_registry` and `provider_status` through the dashboard API.
- Added provider registry/status rendering to the System page and supported
  import provider metadata to the Imports page.
- Threaded provider metadata into setup guidance and capability sources without
  changing advisor calculations or recommendation behaviour.
- Added provider registry, setup/provider metadata, API serialization, and
  advisor-agnostic regression tests.

## PB-006 - Advisor Evidence Quality

- Added shared Evidence Quality contracts for advisor evidence source,
  requirement, unavailable evidence, overall quality, and confidence capping.
- Added evidence summaries to Charging, Cost, Solar, and Efficiency Advisors,
  including required, optional, available, and missing evidence declarations.
- Prevented Charging Advisor from recommending `CHARGE_NOW` from historical
  solar export alone; it now waits when live solar surplus evidence is missing.
- Surfaced advisor evidence quality in Home cards, Glance Mode, Recommendation
  Centre, and advisor API serialization without changing existing API fields.
- Added evidence-quality, advisor regression, Glance, and serialization tests.

## PB-005 - Setup Engine

- Added reusable `SetupEngine` and shared setup checklist, section, task,
  priority, and progress models.
- Derived continuous household setup guidance from existing capability,
  collector health, data freshness, import, goal, and recommendation state.
- Added a Setup page, Home setup progress card, System setup completion metric,
  and Imports upload setup task integration.
- Exposed `setup_progress`, `setup_tasks`, and `setup_sections` through the
  dashboard API and dedicated setup API endpoints.
- Added Setup Engine and API serialization regression tests.

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
