"""The setup flow (PLAN §6.8, D38): find the module, read its socket B, choose the ways in, set each one up. The
reconfigure works on an entry from a menu of its own; the Configure button opens a menu with Settings and Set cloud URL.

1. The module: a greeting, whose Next runs the AT discovery; then one form, a list of the modules that answered with
   "Search again" and "Other IP address" under them, and a field for that IP. A typed IP is probed with the AT
   greeting; when it does not answer, a menu: try again, back to the list, or TCP polling only (no server mode: the
   module does not answer Home Assistant).
2. Socket B, read over the module's AT side: kept as the real cloud endpoint, the one server mode relays to. When the
   module does not answer, or socket B already names Home Assistant, the flow goes on without a cloud (None).
3. The ways in, a menu: the cloud relay (server mode), TCP polling (client mode), both (the cloud relay first), or later
   (an entry with neither: the device, no entities, for the reconfigure).
4. The cloud relay, a form: server mode's port (the first one no other entry listens on), the address the module dials
   Home Assistant by, and the cloud it relays to in a collapsed section. When socket B was read and does not name Home
   Assistant at that port, a menu behind warnings: point it there, leave it to the user's router, or Back to the form.
   Pointing it is STATE-CHANGING and the only change the flow makes to the module on its own: server mode's port opened
   for the wait, socket B set, then the wait for the module to dial in (module_wait.py); when it does not, a menu: wait
   again, revert and start over (STATE-CHANGING, back to the form), or keep it. A module that does not answer gets the
   form without the address: the cloud relay then works only by the user's own redirect in the router.
5. TCP polling, a form: the host and port to connect to, the relay's in practice. Its Next tests the connection at once (the identity read, whose
   serial is kept as the learned one, D36), then a menu: passed, Finish or Back to the form; failed, test again, Ignore
   and finish, or Back.

The reconfigure starts at the entry's module as if it were chosen from the list (the search runs only when another
module is chosen), then a menu over the entry: each way in (when it is on, a menu of its own: change the settings, turn
it off, Back; the cloud relay's turn-off offers to set socket B back to Renac's cloud first when it names Home
Assistant, STATE-CHANGING), Set cloud URL, and forgetting the learned serial (confirmed first). Each of these ends by
saving the entry, which updates and reloads it, but Set cloud URL, which changes only the module and goes back to the
menu. The cloud the entry keeps is never replaced by socket B's value.

Every form's button says Next; a form cannot have a second one, so Back is a menu option after it.

Set cloud URL, in the reconfigure and in the Configure menu: one `host:port` field, filled with socket B as the module
answers when the step opens; its submit sets socket B there, STATE-CHANGING. The cloud the entry keeps stays as it is.

Settings, in the Configure menu: one form of the entry's options, each setting as settings.py lists it, in sections,
Advanced collapsed; the settings of a way in that is off are left out and keep their values. Save stores them as the
options and Home Assistant reloads the entry (OptionsFlowWithReload).

The flow's forms have fields only, no buttons, and Home Assistant's flows have no way back: what is only shown goes in a
step's text, a choice is a menu, whose options act as buttons, Back among them. The texts are for everyone: simple
English, short lines and lists, no emojis but the warnings of the step that points socket B at Home Assistant.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.components import network
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigFlow, ConfigFlowResult, OptionsFlow, OptionsFlowWithReload
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_BIND,
    CONF_CLIENT,
    CONF_CLOUD_HOST,
    CONF_CLOUD_PORT,
    CONF_DNS_SERVER,
    CONF_ENABLED,
    CONF_GIVEN_TITLE,
    CONF_MAC,
    CONF_MODULE,
    CONF_SERIAL,
    CONF_SERVER,
    DOMAIN,
)
from .dashboard import DASHBOARD_KEY
from .module_wait import ModuleWait
from .renac_protocol import AtError, AtSession, ChannelError, FoundModule, Listener, SocketB, dial, discover, read_identity
from .renac_protocol.at_session import BROADCAST, DISCOVER_TIMEOUT_S
from .renac_protocol.const import AT_PORT, CLOUD_PORT, DEFAULT_BIND, DEFAULT_CLIENT_PORT, DEFAULT_DNS_SERVER, DEFAULT_SERVER_PORT
from .renac_protocol.dns import is_ip_literal
from .settings import ADVANCED, options_from_form, settings_shown, ways_on

LOGGER = logging.getLogger(__name__)

BLE_RELAY_URL = "https://github.com/X-Stuff/esp32-ble-relay"
# The README's diagrams, shown in the flow's forms from the repository on GitHub, each a placeholder, as the validator
# refuses URLs in the texts. They need internet on the box: without it a form shows its text alone.
IMAGES_URL = "https://raw.githubusercontent.com/X-Stuff/ha-renac-integration/main/docs/images"
# Renac's cloud as the module has it from the factory: only shown, as a hint, where the cloud is not known.
RENAC_CLOUD = f"germany.le-pv.com:{CLOUD_PORT}"
# The pick form: the choice, a module found or one of these two, and the IP typed for "other".
CONF_CHOICE = "choice"
CONF_IP = "ip"
CHOICE_SEARCH = "search"
CHOICE_OTHER = "other"
# The cloud relay's form: the address the module dials Home Assistant by, and the collapsed section for the cloud server
# mode relays to.
CONF_HA_HOST = "ha_host"
CONF_CLOUD = "cloud"
CONF_CLOUD_URL = "cloud_url"
# What the DNS server field takes for the system's resolver.
SYSTEM_DNS = "system"
# The name Home Assistant answers to on the local network when its local URL names none.
DEFAULT_HA_NAME = "homeassistant.local"
# How long the flow waits for the module to dial in after its socket B was set at Home Assistant.
MODULE_WAIT_S = 30.0

PORT_SELECTOR = NumberSelector(NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX))


def diagram(name: str) -> str:
    """The URL of one of the README's diagrams."""
    return f"{IMAGES_URL}/{name}.svg"


def ha_name(internal_url: str | None) -> str:
    """The name the module dials Home Assistant by: the host of Home Assistant's local network URL when it is a name,
    else homeassistant.local."""
    host = urlparse(internal_url).hostname if internal_url else None
    return host if host and not is_ip_literal(host) else DEFAULT_HA_NAME


def kept_cloud(data: Mapping[str, Any]) -> SocketB | None:
    """The cloud an entry keeps, server mode's cloud host and port; None for no cloud."""
    server = data.get(CONF_SERVER) or {}
    host = server.get(CONF_CLOUD_HOST)
    return SocketB(host, int(server.get(CONF_CLOUD_PORT, CLOUD_PORT))) if host else None


class SetCloudUrl:
    """The step Set cloud URL, in the reconfigure and in the Configure menu. The flow holds the module's IP and the cloud
    kept, and says what follows a set in `_on_cloud_url_set`."""

    _module_ip = ""
    _cloud: SocketB | None = None
    # What the step's text says about the module: where socket B dials, or why the module gave no answer.
    _socket_b_text = ""

    async def async_step_set_cloud_url(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The field, filled with socket B as the module answers when the step opens, empty when it does not; submitted,
        socket B is set to it (STATE-CHANGING). A value socket B already holds sends nothing."""
        errors: dict[str, str] = {}
        value = ""
        if user_input is None:
            try:
                async with AtSession(self._module_ip, AT_PORT) as session:
                    value = (await session.read_socket_b()).address
            except AtError as error:
                LOGGER.warning("the module %s: %s", self._module_ip, error)
                self._socket_b_text = f"- The module does not answer: {error}"
            else:
                self._socket_b_text = f"- The module now sends its data to _{value}_."
        else:
            value = str(user_input.get(CONF_CLOUD_URL, "")).strip()
            try:
                target = SocketB.from_address(value)
            except ValueError:
                errors[CONF_CLOUD_URL] = "bad_address"
            else:
                try:
                    async with AtSession(self._module_ip, AT_PORT) as session:
                        socket_b = await session.set_socket_b(target)
                except AtError as error:
                    LOGGER.warning("the module %s: %s", self._module_ip, error)
                    self._socket_b_text = f"- The module did not take the new address: {error}"
                else:
                    return await self._on_cloud_url_set(socket_b)
        # Markdown: host names and addresses in italics.
        if self._cloud is not None:
            cloud = f"- Renac's cloud, as Home Assistant keeps it: _{self._cloud.address}_."
        else:
            cloud = f"- Home Assistant does not know the address of Renac's cloud. It is usually _{RENAC_CLOUD}_."
        schema = vol.Schema({vol.Required(CONF_CLOUD_URL, default=value): TextSelector()})
        placeholders = {"socket_b": self._socket_b_text, "cloud": cloud}
        return self.async_show_form(step_id="set_cloud_url", data_schema=schema, errors=errors, last_step=False, description_placeholders=placeholders)  # type: ignore[attr-defined]

    async def _on_cloud_url_set(self, socket_b: SocketB) -> ConfigFlowResult:
        raise NotImplementedError


class RenacLocalConfigFlow(SetCloudUrl, ConfigFlow, domain=DOMAIN):
    """The setup flow of the plan's §6.8 and the reconfigure; the entry is created at the setup's end, with no unique id
    (D37)."""

    VERSION = 1

    def __init__(self) -> None:
        # The reconfigure's entry; None while setting up.
        self._entry: ConfigEntry | None = None
        # The modules the search found: those no other entry has, and those another one has, left out of the list.
        self._found: list[FoundModule] = []
        self._added: list[FoundModule] = []
        self._searched = False
        # The MAC the module at `_module_ip` gave in its answer to the greeting; None when it has not answered.
        self._mac: str | None = None
        self._at_error = ""
        # The flow goes on without the module's answer; with TCP polling only, chosen at the probe.
        self._module_silent = False
        self._tcp_only = False
        self._socket_b: SocketB | None = None
        self._socket_b_names_ha = False
        self._dns_server: str | None = DEFAULT_DNS_SERVER
        # The ways in as set up so far: server mode's enabled and port, client mode's enabled, host and port.
        self._server: dict[str, Any] = {}
        self._client: dict[str, Any] = {}
        # TCP polling's form follows the way being set up: both ways, or TCP polling alone once socket B is dealt with.
        self._client_next = False
        # The address the module dials Home Assistant by, as typed in the cloud relay's form.
        self._ha_host = ""
        self._serial: str | None = None
        self._serial_forgotten = False
        self._test_result = ""
        self._test_ok = False
        self._set_error = ""
        self._server_off_result = ""
        # After socket B is set at Home Assistant: its value before, the wait for the module, and what the menu after
        # it says.
        self._socket_b_before: SocketB | None = None
        self._wait: ModuleWait | None = None
        self._wait_task: asyncio.Task[bool] | None = None
        self._no_connection_result = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RenacLocalOptionsFlow()

    # 1. The module

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The greeting; its Next searches."""
        if user_input is not None:
            return await self.async_step_search()
        placeholders = {"ble_relay_url": BLE_RELAY_URL, "image": diagram("overview")}
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}), last_step=False, description_placeholders=placeholders)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The entry's values, then its module as if it were chosen from the list: no search."""
        self._entry = self._get_reconfigure_entry()
        data = self._entry.data
        module = data.get(CONF_MODULE) or {}
        server = data.get(CONF_SERVER) or {}
        self._module_ip = module.get(CONF_HOST, "")
        self._mac = module.get(CONF_MAC)
        self._cloud = self._kept_cloud
        self._dns_server = server.get(CONF_DNS_SERVER, DEFAULT_DNS_SERVER)
        self._server = {CONF_ENABLED: bool(server.get(CONF_ENABLED)), CONF_PORT: int(server.get(CONF_PORT, self._free_port()))}
        self._client = _client(data[CONF_CLIENT]) if data.get(CONF_CLIENT) else {}
        self._serial = data.get(CONF_SERIAL)
        if not self._module_ip:
            return await self.async_step_search()
        return await self._use_module(self._module_ip, self._mac)

    async def async_step_search(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The AT discovery, then the modules that answered, but those another entry has (its module's MAC)."""
        self._searched = True
        found = await discover(BROADCAST, AT_PORT, timeout_s=DISCOVER_TIMEOUT_S)
        macs = {str((entry.data.get(CONF_MODULE) or {}).get(CONF_MAC) or "").lower() for entry in self._other_entries()}
        self._found = [module for module in found if module.mac.lower() not in macs]
        self._added = [module for module in found if module.mac.lower() in macs]
        return await self.async_step_pick()

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The modules found, "Search again" and "Other IP address", and the IP field for the last; Next acts on the
        choice. A typed IP is checked and probed first. The reconfigure's first visit searches first."""
        if user_input is None and not self._searched:
            return await self.async_step_search()
        errors: dict[str, str] = {}
        if user_input is not None:
            choice = user_input.get(CONF_CHOICE)
            ip = str(user_input.get(CONF_IP, "")).strip()
            if choice == CHOICE_SEARCH:
                return await self.async_step_search()
            if choice != CHOICE_OTHER:
                found = next((module for module in self._found if module.ip == choice), None)
                if found is not None:
                    return await self._use_module(found.ip, found.mac)
                errors[CONF_CHOICE] = "gone"
            elif not _is_ipv4(ip):
                errors[CONF_IP] = "not_an_ip"
            else:
                if ip != self._module_ip:
                    self._module_ip, self._mac = ip, None
                return await self.async_step_probe()
        ips = [module.ip for module in self._found]
        options = [SelectOptionDict(value=module.ip, label=f"{module.ip} ({module.mac})") for module in self._found]
        options += [SelectOptionDict(value=CHOICE_SEARCH, label="Search again"), SelectOptionDict(value=CHOICE_OTHER, label="Other IP address (type it below)")]
        # The module chosen before, the entry's in the reconfigure, else the first one found.
        if self._module_ip in ips:
            default = self._module_ip
        elif self._module_ip or not ips:
            default = CHOICE_OTHER
        else:
            default = ips[0]
        schema = vol.Schema(
            {
                vol.Required(CONF_CHOICE, default=default): SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)),
                vol.Optional(CONF_IP, default=self._module_ip): TextSelector(),
            }
        )
        # Markdown: addresses in italics.
        added = f"\nAlready added, so not in the list: {', '.join(f'_{module.ip}_' for module in self._added)}." if self._added else ""
        placeholders = {"count": str(len(self._found)), "added": added}
        return self.async_show_form(step_id="pick", data_schema=schema, errors=errors, last_step=False, description_placeholders=placeholders)

    async def async_step_probe(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The typed IP probed with the AT greeting, which changes nothing; no answer, a menu: again, back to the list,
        or on without the module's answer: TCP polling only, or in the reconfigure the entry's menu."""
        answered = await discover(self._module_ip, AT_PORT, timeout_s=DISCOVER_TIMEOUT_S)
        if answered:
            return await self._use_module(self._module_ip, answered[0].mac)
        LOGGER.warning("the module %s does not answer", self._module_ip)
        without = "no_socket_b" if self._entry is not None else "tcp_only"
        return self.async_show_menu(step_id="probe", menu_options=["probe", "pick", without], description_placeholders={"ip": self._module_ip})

    async def _use_module(self, ip: str, mac: str | None) -> ConfigFlowResult:
        self._module_ip = ip
        self._mac = mac
        self._module_silent = False
        return await self.async_step_socket_b()

    # 2. Socket B

    async def async_step_socket_b(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Reads socket B over the module's AT side; when the module does not answer, a menu: again, another module, or
        on without it."""
        try:
            async with AtSession(self._module_ip, AT_PORT) as session:
                socket_b = await session.read_socket_b()
                if session.module is not None:
                    self._mac = session.module.mac
        except AtError as error:
            LOGGER.warning("the module %s: %s", self._module_ip, error)
            self._at_error = str(error)
            return await self.async_step_no_answer()
        self._socket_b = socket_b
        self._socket_b_names_ha = await self._names_ha(socket_b)
        self._cloud = self._kept_cloud or (None if self._socket_b_names_ha else socket_b)
        return await self._ways_menu()

    async def async_step_no_answer(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The module did not answer: again, another module, or on without it."""
        return self.async_show_menu(
            step_id="no_answer",
            menu_options=["socket_b", "pick", "no_socket_b"],
            description_placeholders={"ip": self._module_ip, "error": self._at_error},
        )

    async def async_step_no_socket_b(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """On without the module's answer: server mode only by the user's own redirect, no socket B; the cloud only as
        the entry keeps it."""
        self._module_silent = True
        self._socket_b = None
        self._cloud = self._kept_cloud
        return await self._ways_menu()

    async def async_step_tcp_only(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Setting up, the module does not answer: TCP polling alone."""
        self._module_silent = True
        self._tcp_only = True
        self._socket_b = None
        self._cloud = None
        return await self.async_step_client()

    async def _ways_menu(self) -> ConfigFlowResult:
        """Setting up, the choice of the ways in; the reconfigure, the entry's menu."""
        return await (self.async_step_overview() if self._entry is not None else self.async_step_ways())

    # 3. The ways in

    async def async_step_ways(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The cloud relay, TCP polling, both (the cloud relay first), or later."""
        placeholders = {"cloud": self._cloud_text()}
        return self.async_show_menu(step_id="ways", menu_options=["server", "client_only", "both", "later"], description_placeholders=placeholders)

    async def async_step_both(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._client_next = True
        return await self.async_step_server()

    async def async_step_client_only(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """TCP polling alone: when socket B names Home Assistant, the offer to set it back first; then its form."""
        self._client_next = True
        return await (self.async_step_sends_to_ha() if self._sends_to_ha() else self.async_step_client())

    async def async_step_later(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Neither way yet: when socket B names Home Assistant, the offer to set it back first; then the entry, its
        device without entities, for the reconfigure."""
        self._client_next = False
        return await self.async_step_sends_to_ha() if self._sends_to_ha() else self._finish()

    def _sends_to_ha(self) -> bool:
        return self._socket_b is not None and self._socket_b_names_ha

    async def async_step_sends_to_ha(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Setting up without the cloud relay, socket B naming Home Assistant: its data would reach no one. Set it back
        to Renac's cloud (Set cloud URL), leave the module as it is, or Back to the ways."""
        placeholders = {"socket_b": self._socket_b.address if self._socket_b is not None else ""}
        return self.async_show_menu(step_id="sends_to_ha", menu_options=["set_cloud_url", "leave_module", "ways"], description_placeholders=placeholders)

    async def async_step_leave_module(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._after_way()

    async def _after_way(self) -> ConfigFlowResult:
        """A way set up, or the module left as it is: TCP polling's form when the ways menu put it next, else the entry,
        created or saved."""
        return await self.async_step_client() if self._client_next else self._finish()

    def _cloud_text(self) -> str:
        """Where the module sends its data, and the cloud server mode relays to."""
        # Markdown: host names and addresses in italics.
        socket_b = self._socket_b
        if self._module_silent:
            where = (
                f"- The module at _{self._module_ip}_ does not answer, so Home Assistant cannot change where it sends its data.\n"
                "- The cloud relay then works only if your router sends the module's data to Home Assistant."
            )
        elif socket_b is None:
            where = "- Home Assistant could not read where the module sends its data."
        elif self._socket_b_names_ha:
            where = f"- The module already sends its data to Home Assistant: _{socket_b.address}_."
        else:
            where = f"- The module now sends its data to _{socket_b.address}_."
        cloud = f"- Renac's cloud: _{self._cloud.address}_." if self._cloud is not None else "- Renac's cloud: not known."
        return f"{where}\n{cloud}"

    # 4. The cloud relay

    async def async_step_server(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The cloud relay's form: the port, the address the module dials Home Assistant by (when the module answers),
        and Renac's cloud. Then the offer to point socket B there when it does not name Home Assistant at that port."""
        errors: dict[str, str] = {}
        if user_input is not None:
            cloud = user_input.get(CONF_CLOUD, {})
            port = int(user_input.get(CONF_PORT, DEFAULT_SERVER_PORT))
            ha_host = str(user_input.get(CONF_HA_HOST, "")).strip()
            dns_server = str(cloud.get(CONF_DNS_SERVER, DEFAULT_DNS_SERVER)).strip()
            cloud_host = str(cloud.get(CONF_CLOUD_HOST, "")).strip()
            if port in self._ports_in_use():
                errors["base"] = "port_taken"
            elif self._socket_b is not None and not _is_host(ha_host, port):
                errors[CONF_HA_HOST] = "bad_host"
            elif dns_server != SYSTEM_DNS and not _is_ipv4(dns_server):
                errors["base"] = "dns_server"
            if not errors:
                self._server = {CONF_ENABLED: True, CONF_PORT: port}
                self._ha_host = ha_host
                self._dns_server = None if dns_server == SYSTEM_DNS else dns_server
                self._cloud = SocketB(cloud_host, int(cloud.get(CONF_CLOUD_PORT, CLOUD_PORT))) if cloud_host else None
                if self._can_point():
                    return await self.async_step_point()
                return await self._after_way()
        placeholders = {"cloud": self._cloud_text(), "detected": await self._detected_ip(), "image": diagram("cloud-relay")}
        return self.async_show_form(step_id="server", data_schema=self._server_schema(), errors=errors, last_step=False, description_placeholders=placeholders)

    def _server_schema(self) -> vol.Schema:
        fields: dict[Any, Any] = {vol.Required(CONF_PORT, default=self._server.get(CONF_PORT) or self._free_port()): PORT_SELECTOR}
        if self._socket_b is not None:
            fields[vol.Required(CONF_HA_HOST, default=self._ha_host or self._default_ha_host())] = TextSelector()
        cloud_fields = {
            vol.Optional(CONF_CLOUD_HOST, default=self._cloud.host if self._cloud else ""): TextSelector(),
            vol.Required(CONF_CLOUD_PORT, default=self._cloud.port if self._cloud else CLOUD_PORT): PORT_SELECTOR,
            vol.Required(CONF_DNS_SERVER, default=self._dns_server or SYSTEM_DNS): TextSelector(),
        }
        fields[vol.Required(CONF_CLOUD)] = section(vol.Schema(cloud_fields), {"collapsed": True})
        return vol.Schema(fields)

    def _default_ha_host(self) -> str:
        """Socket B's host when it names Home Assistant, else the host of Home Assistant's local network URL when it is a
        name, else homeassistant.local."""
        socket_b = self._socket_b
        return socket_b.host if socket_b is not None and self._socket_b_names_ha else ha_name(self.hass.config.internal_url)

    def _can_point(self) -> bool:
        """Server mode on, socket B read, and not naming Home Assistant at server mode's port."""
        socket_b = self._socket_b
        port = self._server.get(CONF_PORT)
        return bool(self._server.get(CONF_ENABLED)) and socket_b is not None and not (self._socket_b_names_ha and socket_b.port == port)

    async def async_step_point(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The offer, behind warnings: point socket B at Home Assistant, leave it to the user's router, or Back to the
        form."""
        placeholders = {
            "socket_b": self._socket_b.address if self._socket_b else "",
            "ha": f"{self._ha_host}:{self._server[CONF_PORT]}",
            "image": diagram("firmware-setting"),
        }
        return self.async_show_menu(step_id="point", menu_options=["point_at_ha", "keep", "server"], description_placeholders=placeholders)

    async def async_step_point_at_ha(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Points socket B at Home Assistant (STATE-CHANGING): server mode's port opened for the wait, socket B set, then
        the wait for the module."""
        port = self._server[CONF_PORT]
        target = SocketB(self._ha_host, port)
        wait = ModuleWait(self._module_ip, port, self._entry_listener(port))
        try:
            await wait.open()
        except OSError as error:
            LOGGER.warning("cannot open port %d: %s", port, error)
            self._set_error = f"Home Assistant cannot open port {port}: {error}"
            return await self.async_step_set_failed()
        try:
            async with AtSession(self._module_ip, AT_PORT) as session:
                await session.set_socket_b(target)
        except (AtError, ValueError) as error:
            await wait.close()
            LOGGER.warning("the module %s: %s", self._module_ip, error)
            self._set_error = str(error)
            return await self.async_step_set_failed()
        self._socket_b_before = self._socket_b
        self._socket_b = target
        self._socket_b_names_ha = True
        self._wait = wait
        return await self.async_step_wait()

    async def async_step_set_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The module did not take the set, or the port could not be opened for the wait: again, leave socket B as it
        is, or Back to the form."""
        return self.async_show_menu(step_id="set_failed", menu_options=["point_at_ha", "keep", "server"], description_placeholders={"error": self._set_error})

    async def async_step_wait(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Waits for the module to dial in, up to MODULE_WAIT_S; then on, or the menu of what to do."""
        if self._wait_task is None:
            self._wait_task = self.hass.async_create_task(self._wait_for_module())
        if not self._wait_task.done():
            placeholders = {"port": str(self._server[CONF_PORT]), "seconds": f"{MODULE_WAIT_S:g}"}
            return self.async_show_progress(step_id="wait", progress_action="wait", progress_task=self._wait_task, description_placeholders=placeholders)
        connected = self._wait_task.result()
        self._wait_task = None
        return self.async_show_progress_done(next_step_id="connected" if connected else "no_connection")

    async def _wait_for_module(self) -> bool:
        wait = self._wait
        assert wait is not None
        try:
            return await wait.wait(MODULE_WAIT_S)
        finally:
            await wait.close()
            self._wait = None

    async def async_step_connected(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The module dialled in."""
        return await self._after_way()

    async def async_step_no_connection(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The module did not dial in: wait again, revert socket B to its value before and start over, or keep it."""
        before = self._socket_b_before.address if self._socket_b_before is not None else ""
        placeholders = {
            "seconds": f"{MODULE_WAIT_S:g}",
            "socket_b": self._socket_b.address if self._socket_b is not None else "",
            "before": before,
            "result": self._no_connection_result,
        }
        options = ["wait_again", "set_back", "keep"] if self._socket_b_before is not None else ["wait_again", "keep"]
        return self.async_show_menu(step_id="no_connection", menu_options=options, description_placeholders=placeholders)

    async def async_step_wait_again(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        port = self._server[CONF_PORT]
        wait = ModuleWait(self._module_ip, port, self._entry_listener(port))
        try:
            await wait.open()
        except OSError as error:
            self._no_connection_result = f"Home Assistant cannot open port {port}: {error}."
            return await self.async_step_no_connection()
        self._no_connection_result = ""
        self._wait = wait
        return await self.async_step_wait()

    async def async_step_set_back(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Socket B reverted to its value before (STATE-CHANGING); then the cloud relay's form again."""
        before = self._socket_b_before
        assert before is not None
        try:
            async with AtSession(self._module_ip, AT_PORT) as session:
                await session.set_socket_b(before)
        except AtError as error:
            LOGGER.warning("the module %s: %s", self._module_ip, error)
            self._no_connection_result = f"The module did not take it: {error}."
            return await self.async_step_no_connection()
        self._socket_b = before
        self._socket_b_names_ha = await self._names_ha(before)
        self._socket_b_before = None
        self._no_connection_result = ""
        return await self.async_step_server()

    async def async_step_keep(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Socket B left as it is, the redirect the user's."""
        return await self._after_way()

    def _entry_listener(self, port: int) -> Listener | None:
        """The reconfigure's entry's listener, when it is loaded and listens on `port`."""
        entry = self._entry
        if entry is None or entry.state is not ConfigEntryState.LOADED or getattr(entry, "runtime_data", None) is None:
            return None
        listener = entry.runtime_data.poller.listener
        return listener if listener is not None and listener.port == port else None

    # 5. TCP polling

    async def async_step_client(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """TCP polling's form: the host and port to connect to; its Next tests the connection."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = _client({**user_input, CONF_ENABLED: True})
            if client[CONF_HOST]:
                self._client = client
                return await self.async_step_test_client()
            errors["base"] = "client_host"
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=self._client.get(CONF_HOST) or self._module_ip): TextSelector(),
                vol.Required(CONF_PORT, default=self._client.get(CONF_PORT, DEFAULT_CLIENT_PORT)): PORT_SELECTOR,
            }
        )
        note = f"The module at _{self._module_ip}_ does not answer, so the cloud relay is off.\n\n" if self._tcp_only else ""
        placeholders = {"note": note, "image": diagram("tcp-polling")}
        return self.async_show_form(step_id="client", data_schema=schema, errors=errors, last_step=False, description_placeholders=placeholders)

    async def async_step_test_client(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Connects and reads the identity, 10000 x 32: no connection, no response, or the model and the serial, which
        is kept as the learned one unless the entry's own is kept; then the menu."""
        host, port = self._client[CONF_HOST], self._client[CONF_PORT]
        self._test_ok = False
        # Markdown: host names and addresses in italics.
        try:
            connection = await dial(host, port)
        except ChannelError as error:
            self._test_result = f"Home Assistant could not connect:\n- {error}\n\nCheck the address and port, and that what listens there is on."
        else:
            try:
                identity = await read_identity(connection)
            finally:
                await connection.close()
            # The entry's own serial, not forgotten, is checked; one this flow's test read before is replaced.
            entry_serial = self._entry.data.get(CONF_SERIAL) if self._entry is not None else None
            if not identity.accepted:
                self._test_result = f"The connection was made, but the inverter did not answer:\n- {identity.error}"
            elif entry_serial is not None and self._serial == entry_serial and identity.serial != entry_serial:
                self._test_result = (
                    "This is another inverter.\n"
                    f"- Its serial number: {identity.serial}\n"
                    f"- This entry is set up for serial number: {entry_serial}\n\n"
                    "To read this inverter instead, go to the settings menu, choose Forget the inverter's serial number, then test again."
                )
            else:
                self._serial = identity.serial
                self._test_ok = True
                self._test_result = f"The inverter answered:\n- Model: {identity.model}\n- Serial number: {identity.serial}"
        return await self.async_step_client_test()

    async def async_step_client_test(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The test's result. It passed: finish, or Back to the form. It did not: test again, ignore it and finish, or
        Back. The reconfigure also offers its settings menu."""
        options = ["finish", "client"] if self._test_ok else ["test_client", "ignore", "client"]
        if self._entry is not None:
            options.append("overview")
        return self.async_show_menu(
            step_id="client_test",
            menu_options=options,
            description_placeholders={"address": f"{self._client[CONF_HOST]}:{self._client[CONF_PORT]}", "result": self._test_result},
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """TCP polling set up: the entry, created or saved."""
        return self._finish()

    async def async_step_ignore(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """TCP polling set up though its test failed: the entry, created or saved."""
        return self._finish()

    # The reconfigure's menu

    async def async_step_overview(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The entry as it is now, and what can be changed: each way in, Set cloud URL, forgetting the serial. Each one
        saves the entry at its end; closing the window keeps the entry as it was."""
        options = ["server_settings", "client_settings"]
        if self._socket_b is not None:
            options.append("set_cloud_url")
        if self._serial is not None:
            options.append("forget_serial")
        return self.async_show_menu(step_id="overview", menu_options=options, description_placeholders={"cloud": self._cloud_text(), "ways": self._ways_text()})

    def _ways_text(self) -> str:
        """The ways in as they are now, and the serial; a warning when the module's data reaches no one."""
        # Markdown: host names and addresses in italics.
        server, client = self._server, self._client
        lines = [
            f"- Cloud relay: on, port {server[CONF_PORT]}." if server.get(CONF_ENABLED) else "- Cloud relay: off.",
            f"- TCP polling: on, _{client[CONF_HOST]}:{client[CONF_PORT]}_." if client.get(CONF_ENABLED) else "- TCP polling: off.",
            f"- This entry's inverter: serial number {self._serial}."
            if self._serial
            else "- This entry's inverter: not known yet. The entry takes the serial number of the inverter at the first connection.",
        ]
        text = "\n".join(lines)
        if not server.get(CONF_ENABLED) and self._socket_b is not None and self._socket_b_names_ha:
            text += "\n\nThe module sends its data to Home Assistant, but the cloud relay is off.\n- So the Renac app gets no data.\n- Use Set the cloud URL."
        if not server.get(CONF_ENABLED) and not client.get(CONF_ENABLED):
            text += "\n\nWith both ways off, Home Assistant reads nothing from the inverter."
        return text

    async def async_step_server_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The cloud relay: on, its menu (change the settings, turn it off, Back); off, its form."""
        if not self._server.get(CONF_ENABLED):
            return await self.async_step_server()
        placeholders = {"port": str(self._server[CONF_PORT])}
        return self.async_show_menu(step_id="server_settings", menu_options=["server", "server_off", "overview"], description_placeholders=placeholders)

    async def async_step_client_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """TCP polling: on, its menu (change the settings, turn it off, Back); off, its form."""
        if not self._client.get(CONF_ENABLED):
            return await self.async_step_client()
        placeholders = {"address": f"{self._client[CONF_HOST]}:{self._client[CONF_PORT]}"}
        return self.async_show_menu(step_id="client_settings", menu_options=["client", "client_off", "overview"], description_placeholders=placeholders)

    async def async_step_server_off(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Turning the cloud relay off: when socket B names Home Assistant, the offer to set it back to Renac's cloud
        first."""
        socket_b = self._socket_b
        if socket_b is None or not self._socket_b_names_ha:
            return await self.async_step_turn_off()
        # Markdown: host names and addresses in italics.
        if self._cloud is not None:
            cloud = f"Home Assistant can set the module back to Renac's cloud: _{self._cloud.address}_."
        else:
            cloud = "Home Assistant does not know the address of Renac's cloud.\n- Use Set the cloud URL in the settings menu."
        options = (["cloud_back"] if self._cloud is not None else []) + ["turn_off", "server_settings"]
        placeholders = {"socket_b": socket_b.address, "cloud": cloud, "result": self._server_off_result}
        return self.async_show_menu(step_id="server_off", menu_options=options, description_placeholders=placeholders)

    async def async_step_cloud_back(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Socket B set back to Renac's cloud (STATE-CHANGING), then the cloud relay off and the entry saved."""
        cloud = self._cloud
        assert cloud is not None
        try:
            async with AtSession(self._module_ip, AT_PORT) as session:
                socket_b = await session.set_socket_b(cloud)
        except (AtError, ValueError) as error:
            LOGGER.warning("the module %s: %s", self._module_ip, error)
            self._server_off_result = f"The module did not take it: {error}."
            return await self.async_step_server_off()
        self._socket_b = socket_b
        self._socket_b_names_ha = await self._names_ha(socket_b)
        self._server_off_result = ""
        return await self.async_step_turn_off()

    async def async_step_turn_off(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The cloud relay off, and the entry saved."""
        self._server = {**self._server, CONF_ENABLED: False}
        return self._finish()

    async def async_step_client_off(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """TCP polling off, and the entry saved."""
        self._client = {**self._client, CONF_ENABLED: False}
        return self._finish()

    async def async_step_forget_serial(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Forgetting the learned serial, to confirm: forget it, or Back."""
        placeholders = {"serial": self._serial or ""}
        return self.async_show_menu(step_id="forget_serial", menu_options=["forget_confirmed", "overview"], description_placeholders=placeholders)

    async def async_step_forget_confirmed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The learned serial forgotten and the entry saved, as Finish does: the next connection's serial is learned."""
        self._serial = None
        self._serial_forgotten = True
        return self._finish()

    async def _on_cloud_url_set(self, socket_b: SocketB) -> ConfigFlowResult:
        """Socket B set: the reconfigure's menu; setting up, on, the address kept as Renac's cloud when none is known."""
        self._socket_b = socket_b
        self._socket_b_names_ha = await self._names_ha(socket_b)
        if self._entry is not None:
            return await self.async_step_overview()
        if self._cloud is None and not self._socket_b_names_ha:
            self._cloud = socket_b
        return await self._after_way()

    # The entry

    def _finish(self) -> ConfigFlowResult:
        """Setting up, the entry created, titled `Renac <module IP>` until the device names it after the inverter; the
        reconfigure, the entry updated and reloaded, its title as it is. A way not set up is written off."""
        cloud = self._cloud
        data = {
            CONF_MODULE: {CONF_HOST: self._module_ip, CONF_MAC: self._mac},
            CONF_SERVER: {
                CONF_ENABLED: bool(self._server.get(CONF_ENABLED)),
                CONF_BIND: DEFAULT_BIND,
                CONF_PORT: self._server.get(CONF_PORT) or self._free_port(),
                CONF_CLOUD_HOST: cloud.host if cloud else None,
                CONF_CLOUD_PORT: cloud.port if cloud else CLOUD_PORT,
                CONF_DNS_SERVER: self._dns_server,
            },
            CONF_CLIENT: self._client or {CONF_ENABLED: False, CONF_HOST: self._module_ip, CONF_PORT: DEFAULT_CLIENT_PORT},
            CONF_SERIAL: self._serial,
        }
        if self._entry is not None:
            entry_data = self._entry.data
            # A serial the running entry learned while the flow was open, unless the flow forgot or read one.
            if self._serial is None and not self._serial_forgotten:
                data[CONF_SERIAL] = entry_data.get(CONF_SERIAL)
            return self.async_update_reload_and_abort(self._entry, data={**data, CONF_GIVEN_TITLE: entry_data.get(CONF_GIVEN_TITLE)})
        title = f"Renac {self._module_ip}"
        return self.async_create_entry(title=title, data={**data, CONF_GIVEN_TITLE: title})

    @property
    def _kept_cloud(self) -> SocketB | None:
        """The cloud the reconfigure's entry keeps, never replaced by socket B's value; None while setting up."""
        return kept_cloud(self._entry.data) if self._entry is not None else None

    def _other_entries(self) -> list[ConfigEntry]:
        """The integration's entries, but the reconfigure's own."""
        return [entry for entry in self._async_current_entries(include_ignore=False) if self._entry is None or entry.entry_id != self._entry.entry_id]

    def _ports_in_use(self) -> set[int]:
        """The ports the other entries' server mode listens on."""
        ports = set()
        for entry in self._other_entries():
            server = entry.data.get(CONF_SERVER) or {}
            if server.get(CONF_ENABLED):
                ports.add(int(server.get(CONF_PORT, DEFAULT_SERVER_PORT)))
        return ports

    def _free_port(self) -> int:
        """20145, or the first port after it no other entry listens on."""
        port = DEFAULT_SERVER_PORT
        in_use = self._ports_in_use()
        while port in in_use:
            port += 1
        return port

    async def _names_ha(self, socket_b: SocketB) -> bool:
        """True when socket B's host is Home Assistant: its name on the local network or one of its addresses."""
        names = {ha_name(self.hass.config.internal_url).lower(), await self._detected_ip()}
        names |= {str(address) for address in await network.async_get_enabled_source_ips(self.hass)}
        return socket_b.host.lower() in names

    async def _detected_ip(self) -> str:
        """Home Assistant's own address toward the module, as its network helper finds it."""
        try:
            return await network.async_get_source_ip(self.hass, target_ip=self._module_ip)
        except HomeAssistantError as error:
            LOGGER.warning("Home Assistant's address toward %s: %s", self._module_ip, error)
            return "unknown"


class RenacLocalOptionsFlow(SetCloudUrl, OptionsFlowWithReload):
    """The Configure button's menu (PLAN §6.8): Settings, Set cloud URL for a module that has answered Home Assistant,
    and Rebuild the Renac dashboard (§6.6). Saved settings reload the entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The menu: Settings; Set cloud URL when the module has answered Home Assistant (the entry knows its MAC);
        Rebuild the Renac dashboard."""
        data = self.config_entry.data
        module = data.get(CONF_MODULE) or {}
        self._module_ip = module.get(CONF_HOST, "")
        self._cloud = kept_cloud(data)
        options = ["settings", *(["set_cloud_url"] if module.get(CONF_MAC) else []), "rebuild_dashboard"]
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_rebuild_dashboard(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The Renac dashboard's views built again from the registry, over the user's edits (dashboard.py)."""
        await self.hass.data[DASHBOARD_KEY].rebuild()
        return self.async_abort(reason="dashboard_rebuilt")

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The settings form (settings.py): each one as the entry's options hold it, or the core's default; those of a
        way in that is off are left out and keep their values. Save stores them as the options; Home Assistant reloads
        the entry."""
        options = self.config_entry.options
        if user_input is not None:
            return self.async_create_entry(data={**options, **options_from_form(user_input)})
        return self.async_show_form(step_id="settings", data_schema=settings_schema(options, ways_on(self.config_entry.data)))

    async def _on_cloud_url_set(self, socket_b: SocketB) -> ConfigFlowResult:
        return self.async_abort(reason="cloud_url_set", description_placeholders={"socket_b": socket_b.address})


def settings_schema(options: Mapping[str, Any], ways: set[str]) -> vol.Schema:
    """The settings form: the settings shown for the ways on, each a switch or a number in its unit, the sections
    after the top fields, Advanced collapsed."""
    top: dict[Any, Any] = {}
    sections: dict[str, dict[Any, Any]] = {}
    for setting in settings_shown(ways):
        field = vol.Required(setting.key, default=setting.shown(options))
        selector: Any = BooleanSelector()
        if not setting.is_switch:
            selector = NumberSelector(NumberSelectorConfig(min=setting.minimum, step="any", mode=NumberSelectorMode.BOX, unit_of_measurement=setting.unit))
        (top if setting.section is None else sections.setdefault(setting.section, {}))[field] = selector
    for name, fields in sections.items():
        top[vol.Required(name)] = section(vol.Schema(fields), {"collapsed": name == ADVANCED})
    return vol.Schema(top)


def _client(fields: dict[str, Any]) -> dict[str, Any]:
    """Client mode's data from its form fields."""
    return {
        CONF_ENABLED: bool(fields.get(CONF_ENABLED)),
        CONF_HOST: str(fields.get(CONF_HOST, "")).strip(),
        CONF_PORT: int(fields.get(CONF_PORT, DEFAULT_CLIENT_PORT)),
    }


def _is_host(host: str, port: int) -> bool:
    """True when socket B may be set to `host:port`."""
    try:
        SocketB(host, port).check()
    except ValueError:
        return False
    return True


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True
