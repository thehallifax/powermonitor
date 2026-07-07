# Sungrow Provider Notes

PowerMonitor includes an opt-in Sungrow measurement provider for local inverter
runtime data. It does not use iSolarCloud or other cloud APIs.

## Configuration

The provider is configured with a local inverter or dongle host/IP and optional
local protocol details:

```python
SungrowMeasurementProvider("192.168.1.70")
SungrowMeasurementProvider.from_config({
    "host": "sungrow.local",
    "port": 502,
    "protocol": "modbus_tcp",
    "unit_id": 1,
})
```

The default local protocol is Modbus TCP on port `502` with unit id `1`.
Sungrow WiNet-S/S2 and logger configurations can vary; some installations use
port `516` or require Modbus TCP to be enabled locally.

## Local Interface

The provider supports injected runtime payloads and an optional Modbus TCP
register-map reader. The register map keeps model-specific addresses out of the
provider core:

```python
SungrowMeasurementProvider(
    "192.168.1.70",
    register_map={
        "pv_power": {"address": 5000, "scale": 1.0},
        "meter_active_power": {"address": 5600, "scale": 1.0, "signed": True},
    },
)
```

When a register map is used, the optional `pymodbus` package must be installed.
Tests use representative fixture payloads, so no hardware is required for
validation.

## Measurement Mapping

PowerMonitor accepts several common runtime field names because Sungrow models,
dongles, and Modbus templates expose slightly different identifiers.

- `daily_pv_generation`, `e_day`, `today_generation`, or `pv_energy_today`
  maps to `solar_generation` in `kWh`.
- `total_dc_power`, `pv_power`, `total_pv_power`, `p_pv`, or `solar_power`
  maps to `solar_generation` current power in `W`.
- `load_power`, `household_power`, `p_load`, `total_load`, or
  `consumption_power` maps to `household_consumption` current power in `W`.
- Direct grid import/export fields map to `grid_import` or `grid_export` in
  `W` with `MeasurementQuality.MEASURED`.
- Signed grid power fields such as `meter_active_power`, `p_grid`,
  `grid_power`, `total_active_power`, or `active_power` are split into
  import/export with `MeasurementQuality.CALCULATED`.
- Direct battery charge/discharge fields map to `battery_charge` or
  `battery_discharge` in `W` with `MeasurementQuality.MEASURED`.
- Signed battery power fields such as `battery_power`, `p_battery`, or
  `pbattery` are split into charge/discharge with
  `MeasurementQuality.CALCULATED`.

## Current Limitations

- The provider is not registered in `DataEngine` by default, so existing
  SolarEdge-backed reporting is unchanged.
- Capability reporting is based on the runtime fields available from the local
  inverter response.
- Real hardware may need local Modbus TCP enabled, and register addresses vary
  by model, firmware, and dongle/logger generation.
- Historical interval import is intentionally deferred until PowerMonitor has
  explicit precedence and storage rules for multiple solar providers.
