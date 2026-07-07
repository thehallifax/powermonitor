# Fronius Provider Notes

PowerMonitor includes an opt-in Fronius measurement provider for the local
Fronius Solar API JSON interface.

## Configuration

The provider is configured with a local inverter host or IP address:

```python
FroniusMeasurementProvider("192.168.1.50")
FroniusMeasurementProvider.from_config({"host": "fronius.local"})
```

If the value does not include a scheme, `http://` is assumed. The provider uses
the local inverter endpoint and does not use Fronius Solar.web cloud access.

## API Endpoint

The first implementation reads:

- `/solar_api/v1/GetPowerFlowRealtimeData.fcgi`

This endpoint provides current site values and, when exposed by the inverter,
daily solar energy.

## Measurement Mapping

- `Site.E_Day` maps to `solar_generation` in `kWh`.
- `Site.P_PV` maps to `solar_generation` current power in `W`.
- `Site.P_Load` maps to `household_consumption` current power in `W` using the
  absolute value, because Fronius power-flow payloads commonly represent load
  as a negative site value.
- Positive `Site.P_Grid` maps to `grid_import` current power in `W`.
- Negative `Site.P_Grid` maps to `grid_export` current power in `W`.

Direct Fronius inverter or meter fields use `MeasurementQuality.MEASURED`.
Grid import/export split from the signed `Site.P_Grid` value uses
`MeasurementQuality.CALCULATED`.

## Current Limitations

- The provider is not registered in `DataEngine` by default, so existing
  SolarEdge-backed reporting is unchanged.
- The local power-flow endpoint is current-state oriented. Historical interval
  import is intentionally deferred until PowerMonitor has explicit precedence
  and storage rules for multiple solar providers.
- Grid import/export and household load depend on the inverter exposing site
  values, usually through a Fronius Smart Meter or compatible meter setup.
