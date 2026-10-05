# Fixtures

`ha_config/` is the configuration of the Home Assistant test instance,
`../tests/ha/compose.yaml`.

The captures of the module talking to Renac's cloud, which
`../docs/PROTOCOL.md` section 7 decodes, are not in this repo: they hold
the unit's serials and network addresses. The tests and the mocks make up
their own data.
