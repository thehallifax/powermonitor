# GoodWe Provider Notes

PowerMonitor includes an opt-in GoodWe measurement provider for local inverter
runtime data. It does not use SEMS/cloud APIs.

## Configuration

The provider is configured with a local inverter host or IP address:

```python
GoodWeMeasurementProvider("192.168.1.60")
GoodWeMeasurementProvider.from_config({"host": "goodwe.local", "port": 8899})
```

The default port is `8899`, matching the well-established local GoodWe UDP
interface used by read-only integrations. Some inverter or firmware families
may expose Modbus/TCP on another port, but this first provider keeps the
configuration simple and host-based.

## Local Interface

The provider can read local runtime values through the optional Python
`goodwe` package when installed. Tests inject representative runtime payloads,
so no hardware is required for validation.

## Measurement Mapping

PowerMonitor accepts several common runtime field names because GoodWe model
families and local libraries expose slightly different identifiers.

- `e_day`, `eday`, `daily_generation`, or `today_pv_generation` maps to
  `solar_generation` in `kWh`.
- `ppv`, `pv_power`, `p_pv`, `total_pv_power`, or `solar_power` maps to
  `solar_generation` current power in `W`.
- `house_consumption`, `load_power`, `p_load`, `total_load`, or
  `consumption_power` maps to `household_consumption` current power in `W`.
- Direct grid import/export fields map to `grid_import` or `grid_export` in
  `W` with `MeasurementQuality.MEASURED`.
- Signed grid power fields such as `pgrid`, `p_grid`, `grid_power`,
  `meter_active_power_total`, or `active_power` are split into import/export
  with `MeasurementQuality.CALCULATED`.
- Signed battery power fields such as `battery_power`, `pbattery`,
  `p_battery`, or `battery_w` are split into `battery_charge` or
  `battery_discharge` in `W` with `MeasurementQuality.CALCULATED`.

## Current Limitations

- The provider is not registered in `DataEngine` by default, so existing
  SolarEdge-backed reporting is unchanged.
- Capability reporting is based on the runtime fields available from the local
  inverter response.
- Real hardware may require firmware support for local readout and network
  reachability to UDP port `8899`.
- Historical interval import is intentionally deferred until PowerMonitor has
  explicit precedence and storage rules for multiple solar providers.
