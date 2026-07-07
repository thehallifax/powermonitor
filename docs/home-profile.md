# Home Profile

The Home Profile is PowerMonitor's persistent description of the household
itself. It is independent of SolarEdge, Home Assistant, Ocular, retailer bills,
or any future measurement provider.

Provider data answers "what did PowerMonitor observe?" The Home Profile answers
"what does this home have?" That distinction lets setup, capability detection,
simulations, recommendations, and future Home Intelligence reason about the
household even before every data source is connected.

## Storage

The profile is stored locally at:

```text
config/home_profile.json
```

If the file does not exist, PowerMonitor returns a sensible default profile. The
default can borrow non-secret context from `config/settings.json`, such as
state/region, retailer, and current plan, but it does not write anything back
unless explicit save functionality is called.

## CLI Setup Wizard

Run the local CLI wizard to create or update the profile:

```sh
python3 collector/setup_home_profile.py
```

The wizard shows existing values as defaults. Press Enter to keep a shown
default, or leave optional fields blank to skip them. It stores declared home
facts only; it does not configure SolarEdge, Home Assistant, Ocular, or any
other live provider.

## Fields

The initial profile captures:

- Location region, such as state or territory.
- Electricity retailer.
- Current tariff.
- Whether solar is installed.
- Optional solar capacity.
- Optional inverter brand and model.
- Whether a battery is installed.
- Optional battery capacity.
- Whether an EV is owned.
- Optional vehicle make and model.
- Optional charger type.
- Whether household monitoring is available.
- Whether Home Assistant is available.

The profile is versioned with `schema_version` and preserves unknown fields when
loaded and saved. This allows future releases to add home metadata without
breaking older profile files.

## Boundary

The Home Profile is not a provider connector and does not perform calculations.
It should not contain credentials, cookies, tokens, account numbers, NMI values,
or raw measurements. It is a local, user-declared profile used as context for
future setup and intelligence work.
