# Enphase Provider Notes

PowerMonitor includes an opt-in Enphase Envoy/IQ Gateway measurement provider
for local gateway data. It does not use Enphase cloud APIs.

## Configuration

The provider is configured with a local Envoy/IQ Gateway host or IP address.
Newer firmware may require a local API token.

```python
EnphaseMeasurementProvider("envoy.local", token="local-token")
EnphaseMeasurementProvider.from_config({
    "host": "192.168.1.80",
    "token": "local-token",
    "use_https": True,
})
```

`username` and `password` can be carried in configuration for future local
authentication workflows, but this provider does not generate Enphase cloud
tokens. Sensitive fields should remain write-only in any UI layer.

## Local Endpoints

The provider reads these local endpoints when available:

- `/production.json`
- `/ivp/meters/readings`

Firmware 7.x and newer commonly require token-based authentication for local
API access. The provider sends `Authorization: Bearer <token>` when a token is
configured. HTTPS is used by default because newer gateways commonly require
it; `use_https=False` can be used for older local setups.

## Measurement Mapping

PowerMonitor supports both `/production.json` and `/ivp/meters/readings`
payload shapes.

- Production `whToday` maps to `solar_generation` in `kWh`.
- Production `wNow` or meter `activePower` maps to `solar_generation` current
  power in `W`.
- Total consumption `wNow` or meter `activePower` maps to
  `household_consumption` current power in `W`.
- `/production.json` `net-consumption.wNow` is split into `grid_import` when
  positive and `grid_export` when negative.
- `/ivp/meters/readings` `net-consumption.activePower` is split into
  `grid_export` when positive and `grid_import` when negative, matching the
  meter convention where positive active power is delivered through the CT.
- Storage `wNow` maps to `battery_discharge` when positive and
  `battery_charge` when negative.

Direct Envoy production and consumption fields use
`MeasurementQuality.MEASURED`. Signed net-consumption and storage direction
splits use `MeasurementQuality.CALCULATED`.

## Current Limitations

- The provider is not registered in `DataEngine` by default, so existing
  SolarEdge-backed reporting is unchanged.
- Capability reporting is based on the runtime fields available from the local
  Envoy response.
- Local endpoint availability varies by Envoy/IQ Gateway model, firmware,
  metering hardware, and token permissions.
- Historical interval import is intentionally deferred until PowerMonitor has
  explicit precedence and storage rules for multiple solar providers.
