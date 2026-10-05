"""Names shared by the integration's modules."""

DOMAIN = "renac_local"

# The entry's data: the module the setup flow found (host, mac), the two ways in, the serial learned at the first
# connection, and the title the integration gave the entry last: while the entry's title is that one, the user has not
# renamed it. Server mode: enabled, bind, port, and the cloud it relays (cloud_host, none for no cloud; cloud_port;
# dns_server, none for the system's resolver). Client mode: enabled, host, port.
CONF_MODULE = "module"
CONF_SERVER = "server"
CONF_CLIENT = "client"
CONF_ENABLED = "enabled"
CONF_BIND = "bind"
CONF_CLOUD_HOST = "cloud_host"
CONF_CLOUD_PORT = "cloud_port"
CONF_DNS_SERVER = "dns_server"
CONF_MAC = "mac"
CONF_SERIAL = "serial"
CONF_GIVEN_TITLE = "given_title"

# Fired for every cloud write outside the clock, blocked or forwarded, with a notification (PLAN §6.5, D18).
EVENT_CLOUD_WRITE = f"{DOMAIN}_cloud_write"

# The registers the device shows, not entities of their own: the model, the serial, the firmware.
DEVICE_KEYS = ("model", "serial", "master_version", "slave_version", "hmi_version")
# The registers shown as binary sensors: the BMS and Meter 1 connected, the battery's charging and discharging forbidden.
BINARY_KEYS = ("bms_connected", "meter_connected", "charge_forbidden", "discharge_forbidden")
