"""The flows (PLAN §6.8) and the removal's check against the fake module's AT side and the mock relay on 127.0.0.1;
the discovery goes to the fake's port on 127.0.0.1, never a broadcast. Setting up: the cloud relay with socket B
pointed at Home Assistant, Back from its offer; the module not dialling in, waited for again and reverted; nothing
found, searching again and a typed IP probed; a module another entry has; set up later; a typed IP that does not answer
and TCP polling alone, its test and Back; a module whose socket B cannot be read; the cloud relay's checks and the next
free port; both ways, socket B already naming Home Assistant, the cloud typed. The reconfigure: the entry's own port
watched; the cloud kept, the serial forgotten, the cloud relay turned off with socket B set back; a module that does
not answer with server mode turned on, and another inverter's serial. From the Configure menu: Set cloud URL, and the
Settings reaching the core's blocks and the entities after the reload, TCP polling's and the cloud relay's. The
Repairs issue's Fix; the issue gone after a reconfigure; the removal's check of socket B."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.components.repairs import FlowType, repairs_flow_manager
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult, FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.renac_local import config_flow, issues, removal
from custom_components.renac_local.config_flow import CONF_CHOICE, CONF_CLOUD, CONF_CLOUD_URL, CONF_HA_HOST, CONF_IP
from custom_components.renac_local.const import (
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
from tests.ha.common import entry, free_port, groups_shown, set_up, wait_until
from tests.mocks.mock_hf_lpt270 import AT_GREETING, AtServer, FakeModule, Identity, SimulatedInverter, TcpModule, initial_words
from tests.mocks.mock_relay import MockRelay

# Home Assistant's address in the test harness: its network adapters and its source address are mocked.
HA_IP = "10.10.10.10"
NO_CLIENT = {CONF_ENABLED: False, CONF_HOST: "127.0.0.1", CONF_PORT: 8899}
NO_CLOUD = {CONF_CLOUD_HOST: "", CONF_CLOUD_PORT: 20145, CONF_DNS_SERVER: "8.8.8.8"}
# The cloud an entry keeps in these tests.
KEPT_CLOUD = {CONF_CLOUD_HOST: "cloud.example", CONF_CLOUD_PORT: 20146, CONF_DNS_SERVER: "8.8.8.8"}
SETUP_ENTRY = "custom_components.renac_local.async_setup_entry"


def nobody_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
async def module(socket_enabled: None, identity: Identity, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TcpModule]:
    """The fake module's AT side on 127.0.0.1, the flows' and the removal's AT port and the discovery pointed at it; its
    socket B, not running, dials 127.0.0.1:20146."""
    socket_b = TcpModule(FakeModule(SimulatedInverter(initial_words(identity)), identity=identity), "127.0.0.1", 20146)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: AtServer(socket_b, identity), local_addr=("127.0.0.1", 0))
    at_port = transport.get_extra_info("sockname")[1]
    monkeypatch.setattr(config_flow, "AT_PORT", at_port)
    monkeypatch.setattr(removal, "AT_PORT", at_port)
    monkeypatch.setattr(config_flow, "BROADCAST", "127.0.0.1")
    monkeypatch.setattr(config_flow, "DISCOVER_TIMEOUT_S", 0.2)
    yield socket_b
    transport.close()


async def search(hass: HomeAssistant) -> FlowResult:
    """The greeting, its Next, and the pick form after the search."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user" and result["last_step"] is False
    result = await submit(hass, result, {})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pick"
    return result


async def the_ways(hass: HomeAssistant) -> FlowResult:
    """Setting up as far as the ways menu, with the module found."""
    result = await submit(hass, await search(hass), {CONF_CHOICE: "127.0.0.1", CONF_IP: ""})
    assert result["step_id"] == "ways" and result["menu_options"] == ["server", "client_only", "both", "later"]
    return result


async def submit(hass: HomeAssistant, result: FlowResult, user_input: dict[str, Any]) -> FlowResult:
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def pick(hass: HomeAssistant, result: FlowResult, step_id: str) -> FlowResult:
    assert result["type"] is FlowResultType.MENU, result
    return await submit(hass, result, {"next_step_id": step_id})


def default_of(result: FlowResult, *path: str) -> Any:
    """A field's default in the form, through its sections."""
    schema = result["data_schema"]
    for name in path:
        key = next(key for key in schema.schema if key == name)
        if name == path[-1]:
            return key.default()
        schema = schema.schema[key].schema
    raise AssertionError(path)


def fields(result: FlowResult) -> list[str]:
    return [str(key) for key in result["data_schema"].schema]


def section_fields(result: FlowResult, name: str) -> list[str]:
    """The fields of the form's section `name`."""
    schema = result["data_schema"]
    found = next(value for key, value in schema.schema.items() if key == name)
    return [str(key) for key in found.schema.schema]


def choices(result: FlowResult) -> list[str]:
    """The pick form's choices."""
    selector = next(value for key, value in result["data_schema"].schema.items() if key == CONF_CHOICE)
    return [option["value"] for option in selector.config["options"]]


def server_form(port: int, ha_host: str | None = None, cloud: dict[str, Any] = NO_CLOUD) -> dict[str, Any]:
    """The cloud relay's form as submitted; `ha_host` only where the form has it."""
    return {CONF_PORT: port, CONF_CLOUD: cloud} | ({CONF_HA_HOST: ha_host} if ha_host is not None else {})


def flow_entry(
    *, mac: str | None, server: bool, port: int = 20145, client: dict[str, Any] = NO_CLIENT, cloud: dict[str, Any] = KEPT_CLOUD, serial: str | None = None
) -> MockConfigEntry:
    """An entry as the setup flow writes it, for the module on 127.0.0.1, server mode on `port`, the cloud kept
    `cloud`."""
    data = {
        CONF_MODULE: {CONF_HOST: "127.0.0.1", CONF_MAC: mac},
        CONF_SERVER: {CONF_ENABLED: server, CONF_BIND: "0.0.0.0", CONF_PORT: port, **cloud},
        CONF_CLIENT: client,
        CONF_SERIAL: serial,
    }
    return MockConfigEntry(domain=DOMAIN, title="Renac 127.0.0.1", data=data)


async def reconfigure(hass: HomeAssistant, config: MockConfigEntry) -> FlowResult:
    """The entry's reconfigure: it starts at the entry's module, socket B read at once."""
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": config.entry_id})


async def done_waiting(hass: HomeAssistant, result: FlowResult) -> FlowResult:
    """The wait for the module, through to the step after it. A module that dialled in during the set ends the wait at
    once, and Home Assistant goes on to the next step by itself: `result` is that step's already."""
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        assert result["progress_action"] == "wait"
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def notifications(hass: HomeAssistant) -> dict[str, str]:
    """The messages of the notifications created from now on, by id."""
    shown: dict[str, str] = {}

    @callback
    def on_notifications(update: persistent_notification.UpdateType, changed: dict[str, persistent_notification.Notification]) -> None:
        if update is not persistent_notification.UpdateType.REMOVED:
            shown.update({key: notification["message"] for key, notification in changed.items()})

    persistent_notification.async_register_callback(hass, on_notifications)
    return shown


async def test_the_cloud_relay_with_socket_b_pointed_at_ha(hass: HomeAssistant, module: TcpModule, identity: Identity) -> None:
    result = await search(hass)
    assert choices(result) == ["127.0.0.1", "search", "other"] and default_of(result, CONF_CHOICE) == "127.0.0.1"
    result = await submit(hass, result, {CONF_CHOICE: "127.0.0.1", CONF_IP: ""})
    assert result["step_id"] == "ways" and "127.0.0.1:20146" in result["description_placeholders"]["cloud"]
    result = await pick(hass, result, "server")
    assert (default_of(result, CONF_PORT), default_of(result, CONF_HA_HOST)) == (20145, "homeassistant.local")
    assert result["description_placeholders"]["detected"] == HA_IP
    cloud = {name: default_of(result, CONF_CLOUD, name) for name in (CONF_CLOUD_HOST, CONF_CLOUD_PORT, CONF_DNS_SERVER)}
    assert cloud == {CONF_CLOUD_HOST: "127.0.0.1", CONF_CLOUD_PORT: 20146, CONF_DNS_SERVER: "8.8.8.8"}

    port = free_port()
    result = await submit(hass, result, server_form(port, "bad host", cloud))
    assert result["errors"] == {CONF_HA_HOST: "bad_host"}
    result = await submit(hass, result, server_form(port, "127.0.0.1", cloud))
    placeholders = result["description_placeholders"]
    assert result["step_id"] == "point" and (placeholders["socket_b"], placeholders["ha"]) == ("127.0.0.1:20146", f"127.0.0.1:{port}")
    # Back to the form, with what was typed; nothing changed on the module.
    result = await pick(hass, result, "server")
    assert result["step_id"] == "server" and default_of(result, CONF_HA_HOST) == "127.0.0.1" and (module.host, module.port) == ("127.0.0.1", 20146)
    result = await submit(hass, result, server_form(port, "127.0.0.1", cloud))

    # The module, running, dials Home Assistant as soon as its socket B is set.
    running = asyncio.create_task(module.run())
    try:
        with patch(SETUP_ENTRY, return_value=True):
            result = await done_waiting(hass, await pick(hass, result, "point_at_ha"))
    finally:
        await stop(running)
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["title"] == "Renac 127.0.0.1"
    assert (module.host, module.port) == ("127.0.0.1", port)
    assert result["data"] == {
        CONF_MODULE: {CONF_HOST: "127.0.0.1", CONF_MAC: identity.mac.replace(":", "")},
        CONF_SERVER: {
            CONF_ENABLED: True,
            CONF_BIND: "0.0.0.0",
            CONF_PORT: port,
            CONF_CLOUD_HOST: "127.0.0.1",
            CONF_CLOUD_PORT: 20146,
            CONF_DNS_SERVER: "8.8.8.8",
        },
        CONF_CLIENT: NO_CLIENT,
        CONF_SERIAL: None,
        CONF_GIVEN_TITLE: "Renac 127.0.0.1",
    }
    assert result["result"].unique_id is None


async def test_a_module_that_does_not_dial_in_is_waited_for_again_and_reverted(hass: HomeAssistant, module: TcpModule, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_flow, "MODULE_WAIT_S", 0.2)
    port = free_port()
    result = await submit(hass, await pick(hass, await the_ways(hass), "server"), server_form(port, "127.0.0.1"))
    # Socket B moves, but the module, not running, never dials in.
    result = await pick(hass, result, "point_at_ha")
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    result = await done_waiting(hass, result)
    assert result["step_id"] == "no_connection" and result["menu_options"] == ["wait_again", "set_back", "keep"]
    assert result["description_placeholders"]["before"] == "127.0.0.1:20146" and (module.host, module.port) == ("127.0.0.1", port)
    # The wait's port is closed after it.
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)

    result = await done_waiting(hass, await pick(hass, result, "wait_again"))
    assert result["step_id"] == "no_connection"
    result = await pick(hass, result, "set_back")
    assert result["step_id"] == "server" and (module.host, module.port) == ("127.0.0.1", 20146)


async def test_nothing_found_search_again_and_a_typed_ip(hass: HomeAssistant, module: TcpModule, monkeypatch: pytest.MonkeyPatch) -> None:
    at_port = config_flow.AT_PORT
    monkeypatch.setattr(config_flow, "AT_PORT", nobody_port())
    result = await search(hass)
    assert choices(result) == ["search", "other"] and default_of(result, CONF_CHOICE) == "other"
    assert result["description_placeholders"]["count"] == "0"
    result = await submit(hass, result, {CONF_CHOICE: "other", CONF_IP: "the module"})
    assert result["errors"] == {CONF_IP: "not_an_ip"}

    monkeypatch.setattr(config_flow, "AT_PORT", at_port)
    result = await submit(hass, result, {CONF_CHOICE: "search", CONF_IP: ""})
    assert result["step_id"] == "pick" and choices(result) == ["127.0.0.1", "search", "other"]
    # A typed IP that answers the probe goes on like a module found.
    result = await submit(hass, result, {CONF_CHOICE: "other", CONF_IP: "127.0.0.1"})
    assert result["step_id"] == "ways"


async def test_a_module_another_entry_has_is_not_listed_and_set_up_later(hass: HomeAssistant, module: TcpModule, identity: Identity) -> None:
    flow_entry(mac=identity.mac.replace(":", "").lower(), server=True).add_to_hass(hass)
    result = await search(hass)
    assert choices(result) == ["search", "other"] and "_127.0.0.1_" in result["description_placeholders"]["added"]
    # Typed, it can still be added again.
    result = await submit(hass, result, {CONF_CHOICE: "other", CONF_IP: "127.0.0.1"})
    assert result["step_id"] == "ways"
    # Later: neither way, for the reconfigure; the port is the next free one.
    with patch(SETUP_ENTRY, return_value=True):
        result = await pick(hass, result, "later")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (result["data"][CONF_SERVER][CONF_ENABLED], result["data"][CONF_SERVER][CONF_PORT], result["data"][CONF_CLIENT]) == (False, 20146, NO_CLIENT)


async def test_a_typed_ip_without_an_answer_and_tcp_polling_alone(
    hass: HomeAssistant, module: TcpModule, relay: MockRelay, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_flow, "AT_PORT", nobody_port())
    result = await search(hass)
    result = await submit(hass, result, {CONF_CHOICE: "other", CONF_IP: "127.0.0.1"})
    assert result["step_id"] == "probe" and result["description_placeholders"]["ip"] == "127.0.0.1"
    result = await pick(hass, result, "probe")
    assert result["step_id"] == "probe"
    result = await pick(hass, result, "tcp_only")
    assert result["step_id"] == "client" and "does not answer" in result["description_placeholders"]["note"]
    result = await submit(hass, result, {CONF_HOST: " ", CONF_PORT: 8899})
    assert result["errors"] == {"base": "client_host"}

    # A wrong port: tested at once; Back to the form with it.
    result = await submit(hass, result, {CONF_HOST: "127.0.0.1", CONF_PORT: 1})
    assert result["step_id"] == "client_test" and "could not connect" in result["description_placeholders"]["result"]
    assert result["menu_options"] == ["test_client", "ignore", "client"]
    result = await pick(hass, result, "client")
    assert result["step_id"] == "client" and default_of(result, CONF_PORT) == 1
    result = await submit(hass, result, {CONF_HOST: "127.0.0.1", CONF_PORT: relay.port})
    assert result["step_id"] == "client_test" and identity.inverter_serial in result["description_placeholders"]["result"]
    assert identity.model in result["description_placeholders"]["result"] and result["menu_options"] == ["finish", "client"]

    with patch(SETUP_ENTRY, return_value=True):
        result = await pick(hass, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODULE] == {CONF_HOST: "127.0.0.1", CONF_MAC: None}
    assert result["data"][CONF_SERVER][CONF_ENABLED] is False and result["data"][CONF_SERVER][CONF_CLOUD_HOST] is None
    assert result["data"][CONF_CLIENT] == {CONF_ENABLED: True, CONF_HOST: "127.0.0.1", CONF_PORT: relay.port}
    assert result["data"][CONF_SERIAL] == identity.inverter_serial


async def test_socket_b_that_cannot_be_read(hass: HomeAssistant, module: TcpModule, monkeypatch: pytest.MonkeyPatch) -> None:
    # The module answers the search, but not the AT session's commands.
    answer = AtServer.answer
    monkeypatch.setattr(AtServer, "answer", lambda self, data, address: answer(self, data, address) if data == AT_GREETING else None)
    result = await search(hass)
    result = await submit(hass, result, {CONF_CHOICE: "127.0.0.1", CONF_IP: ""})
    assert result["step_id"] == "no_answer" and "no reply" in result["description_placeholders"]["error"]
    result = await pick(hass, result, "no_socket_b")
    assert result["step_id"] == "ways" and "does not answer" in result["description_placeholders"]["cloud"]

    # The cloud relay without socket B: no address to point it at, no offer.
    result = await pick(hass, result, "server")
    assert CONF_HA_HOST not in fields(result) and default_of(result, CONF_CLOUD, CONF_CLOUD_HOST) == ""
    with patch(SETUP_ENTRY, return_value=True):
        result = await submit(hass, result, server_form(20145))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERVER][CONF_ENABLED] is True and result["data"][CONF_SERVER][CONF_CLOUD_HOST] is None


async def test_the_cloud_relays_checks_and_the_next_free_port(hass: HomeAssistant, module: TcpModule) -> None:
    MockConfigEntry(domain=DOMAIN, data={CONF_SERVER: {CONF_ENABLED: True, CONF_PORT: 20145}}).add_to_hass(hass)
    result = await pick(hass, await the_ways(hass), "server")
    assert default_of(result, CONF_PORT) == 20146
    result = await submit(hass, result, server_form(20145, "homeassistant.local"))
    assert result["errors"] == {"base": "port_taken"}
    result = await submit(hass, result, server_form(20146, "homeassistant.local", {**NO_CLOUD, CONF_DNS_SERVER: "the system's"}))
    assert result["errors"] == {"base": "dns_server"}


async def test_both_ways_with_socket_b_naming_ha_and_the_cloud_typed(hass: HomeAssistant, module: TcpModule, relay: MockRelay) -> None:
    module.host, module.port = HA_IP, 20145
    result = await the_ways(hass)
    assert "already sends its data to Home Assistant" in result["description_placeholders"]["cloud"]
    result = await pick(hass, result, "both")
    assert default_of(result, CONF_HA_HOST) == HA_IP and default_of(result, CONF_CLOUD, CONF_CLOUD_HOST) == ""

    # Socket B already names Home Assistant at server mode's port: no offer, TCP polling next.
    cloud = {CONF_CLOUD_HOST: "cloud.example", CONF_CLOUD_PORT: 20145, CONF_DNS_SERVER: "system"}
    result = await submit(hass, result, server_form(20145, HA_IP, cloud))
    assert result["step_id"] == "client"
    result = await submit(hass, result, {CONF_HOST: "127.0.0.1", CONF_PORT: relay.port})
    with patch(SETUP_ENTRY, return_value=True):
        result = await pick(hass, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERVER] == {
        CONF_ENABLED: True,
        CONF_BIND: "0.0.0.0",
        CONF_PORT: 20145,
        CONF_CLOUD_HOST: "cloud.example",
        CONF_CLOUD_PORT: 20145,
        CONF_DNS_SERVER: None,
    }
    assert result["data"][CONF_CLIENT] == {CONF_ENABLED: True, CONF_HOST: "127.0.0.1", CONF_PORT: relay.port}


async def test_tcp_polling_alone_while_the_module_sends_to_ha_offers_to_set_it_back(hass: HomeAssistant, module: TcpModule, relay: MockRelay) -> None:
    module.host, module.port = HA_IP, 20145
    result = await pick(hass, await the_ways(hass), "client_only")
    assert result["step_id"] == "sends_to_ha" and result["menu_options"] == ["set_cloud_url", "leave_module", "ways"]
    # Back to the ways and on again; then the module set back, the address typed kept as Renac's cloud.
    result = await pick(hass, await pick(hass, result, "ways"), "client_only")
    result = await pick(hass, result, "set_cloud_url")
    assert default_of(result, CONF_CLOUD_URL) == f"{HA_IP}:20145" and "germany.le-pv.com:20145" in result["description_placeholders"]["cloud"]
    result = await submit(hass, result, {CONF_CLOUD_URL: "cloud.example:20146"})
    assert result["step_id"] == "client" and (module.host, module.port) == ("cloud.example", 20146)
    result = await submit(hass, result, {CONF_HOST: "127.0.0.1", CONF_PORT: relay.port})
    with patch(SETUP_ENTRY, return_value=True):
        result = await pick(hass, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    server = result["data"][CONF_SERVER]
    assert (server[CONF_ENABLED], server[CONF_CLOUD_HOST], server[CONF_CLOUD_PORT]) == (False, "cloud.example", 20146)


async def test_the_reconfigure_waits_on_the_entrys_own_port(hass: HomeAssistant, module: TcpModule, identity: Identity) -> None:
    port = free_port()
    no_cloud = {CONF_CLOUD_HOST: None, CONF_CLOUD_PORT: 20145, CONF_DNS_SERVER: None}
    config = flow_entry(mac=identity.mac.replace(":", ""), server=True, port=port, cloud=no_cloud)
    # Loaded: the entry listens on the port, so the wait watches its listener.
    await set_up(hass, config)
    running = asyncio.create_task(module.run())
    try:
        result = await reconfigure(hass, config)
        assert result["step_id"] == "overview" and result["menu_options"] == ["server_settings", "client_settings", "set_cloud_url"]
        result = await pick(hass, result, "server_settings")
        assert result["step_id"] == "server_settings" and result["menu_options"] == ["server", "server_off", "overview"]
        result = await pick(hass, result, "server")
        assert default_of(result, CONF_PORT) == port
        result = await submit(hass, result, server_form(port, "127.0.0.1"))
        # The module dialled in: the entry saved, the flow closed.
        result = await done_waiting(hass, await pick(hass, result, "point_at_ha"))
        await hass.async_block_till_done()
    finally:
        await stop(running)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert (module.host, module.port) == ("127.0.0.1", port)


async def test_the_reconfigure_keeps_the_cloud_forgets_the_serial_and_turns_the_cloud_relay_off(
    hass: HomeAssistant, module: TcpModule, identity: Identity
) -> None:
    module.host, module.port = HA_IP, 20145
    config = flow_entry(mac=identity.mac.replace(":", ""), server=True, serial="1234567890123456")
    config.add_to_hass(hass)
    # No search: the entry's module, its socket B read at once.
    result = await reconfigure(hass, config)
    assert result["step_id"] == "overview"
    assert result["menu_options"] == ["server_settings", "client_settings", "set_cloud_url", "forget_serial"]
    assert "Renac's cloud: _cloud.example:20146_" in result["description_placeholders"]["cloud"]
    assert "This entry's inverter: serial number 1234567890123456" in result["description_placeholders"]["ways"]

    # The cloud relay's settings: the entry's port, the cloud it keeps; socket B names Home Assistant there: no offer,
    # the entry saved.
    result = await pick(hass, await pick(hass, result, "server_settings"), "server")
    assert (default_of(result, CONF_PORT), default_of(result, CONF_HA_HOST), default_of(result, CONF_CLOUD, CONF_CLOUD_HOST)) == (20145, HA_IP, "cloud.example")
    with patch(SETUP_ENTRY, return_value=True):
        result = await submit(hass, result, server_form(20145, HA_IP, KEPT_CLOUD))
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"

    # Set cloud URL to the value socket B holds: nothing sent, back to the menu.
    result = await submit(hass, await pick(hass, await reconfigure(hass, config), "set_cloud_url"), {CONF_CLOUD_URL: f"{HA_IP}:20145"})
    assert result["step_id"] == "overview"
    # Turning the cloud relay off, in its own menu, while socket B names Home Assistant: the offer to set it back to
    # Renac's cloud; then the entry saved.
    result = await pick(hass, await pick(hass, result, "server_settings"), "server_off")
    assert result["step_id"] == "server_off" and result["menu_options"] == ["cloud_back", "turn_off", "server_settings"]
    with patch(SETUP_ENTRY, return_value=True):
        result = await pick(hass, result, "cloud_back")
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and (module.host, module.port) == ("cloud.example", 20146)
    assert config.data[CONF_SERVER][CONF_ENABLED] is False

    # Forgetting the serial asks first; Back keeps it; confirmed, it is forgotten and the entry saved with it.
    result = await pick(hass, await reconfigure(hass, config), "forget_serial")
    assert result["step_id"] == "forget_serial" and result["menu_options"] == ["forget_confirmed", "overview"]
    result = await pick(hass, await pick(hass, result, "overview"), "forget_serial")
    with patch(SETUP_ENTRY, return_value=True):
        result = await pick(hass, result, "forget_confirmed")
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert config.data[CONF_SERIAL] is None and config.data[CONF_SERVER] == {CONF_ENABLED: False, CONF_BIND: "0.0.0.0", CONF_PORT: 20145, **KEPT_CLOUD}


async def test_the_reconfigure_of_a_module_that_does_not_answer_turns_server_mode_on(
    hass: HomeAssistant, module: TcpModule, relay: MockRelay, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_flow, "AT_PORT", nobody_port())
    client = {CONF_ENABLED: True, CONF_HOST: "127.0.0.1", CONF_PORT: relay.port}
    config = flow_entry(mac=None, server=False, client=client, serial="0" * 16)
    config.add_to_hass(hass)
    result = await reconfigure(hass, config)
    assert result["step_id"] == "no_answer" and result["menu_options"] == ["socket_b", "pick", "no_socket_b"]
    # Another module: the search runs now.
    result = await pick(hass, result, "pick")
    assert (choices(result), default_of(result, CONF_CHOICE), default_of(result, CONF_IP)) == (["search", "other"], "other", "127.0.0.1")
    result = await submit(hass, result, {CONF_CHOICE: "other", CONF_IP: "127.0.0.1"})
    assert result["step_id"] == "probe" and result["menu_options"] == ["probe", "pick", "no_socket_b"]
    result = await pick(hass, result, "no_socket_b")
    assert result["step_id"] == "overview" and "does not answer" in result["description_placeholders"]["cloud"]
    assert result["menu_options"] == ["server_settings", "client_settings", "forget_serial"]

    # Server mode, off, turned on for the user's own redirect: its form at once, no address, no offer; the entry saved.
    result = await pick(hass, result, "server_settings")
    assert result["step_id"] == "server" and CONF_HA_HOST not in fields(result)
    with patch(SETUP_ENTRY, return_value=True):
        result = await submit(hass, result, server_form(20145))
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and config.data[CONF_SERVER][CONF_ENABLED] is True

    result = await pick(hass, await reconfigure(hass, config), "no_socket_b")
    assert "Cloud relay: on, port 20145" in result["description_placeholders"]["ways"]
    result = await pick(hass, await pick(hass, result, "client_settings"), "client")
    assert (default_of(result, CONF_HOST), default_of(result, CONF_PORT)) == ("127.0.0.1", relay.port)
    result = await submit(hass, result, {CONF_HOST: "127.0.0.1", CONF_PORT: relay.port})
    # Another inverter than the entry's: shown, and the entry's serial kept.
    assert f"another inverter.\n- Its serial number: {identity.inverter_serial}" in result["description_placeholders"]["result"]
    assert result["menu_options"] == ["test_client", "ignore", "client", "overview"]
    with patch(SETUP_ENTRY, return_value=True):
        result = await pick(hass, result, "ignore")
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert config.data[CONF_SERVER][CONF_ENABLED] is True and config.data[CONF_SERIAL] == "0" * 16


async def test_set_cloud_url_from_the_configure_menu(hass: HomeAssistant, module: TcpModule, identity: Identity) -> None:
    config = flow_entry(mac=identity.mac.replace(":", ""), server=True)
    config.add_to_hass(hass)
    data = dict(config.data)
    result = await hass.config_entries.options.async_init(config.entry_id)
    assert result["type"] is FlowResultType.MENU and result["menu_options"] == ["settings", "set_cloud_url", "rebuild_dashboard"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "set_cloud_url"})
    assert default_of(result, CONF_CLOUD_URL) == "127.0.0.1:20146"
    assert "_127.0.0.1:20146_" in result["description_placeholders"]["socket_b"]
    assert "_cloud.example:20146_" in result["description_placeholders"]["cloud"]

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_CLOUD_URL: "cloud.example"})
    assert result["errors"] == {CONF_CLOUD_URL: "bad_address"} and (module.host, module.port) == ("127.0.0.1", 20146)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_CLOUD_URL: "ha.example:20145"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cloud_url_set"
    # Socket B moved; the cloud the entry keeps stays as it is.
    assert (module.host, module.port) == ("ha.example", 20145) and config.data == data

    # A module that never answered: nothing to set; the Settings alone.
    silent = flow_entry(mac=None, server=False)
    silent.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(silent.entry_id)
    assert result["type"] is FlowResultType.MENU and result["menu_options"] == ["settings", "rebuild_dashboard"]


async def test_the_settings_reach_the_blocks_and_the_entities_after_the_reload(hass: HomeAssistant, relay: MockRelay, identity: Identity) -> None:
    config = entry(client=relay.port, serial=identity.inverter_serial)
    device = await set_up(hass, config)
    await wait_until(lambda: groups_shown(device))
    options = hass.config_entries.options
    result = await options.async_init(config.entry_id)
    result = await options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "settings"
    # TCP polling alone: its quiet time is shown, the cloud relay's settings are not; a time in the form's own unit.
    assert "quiet_time_s" in fields(result) and "forward_writes" not in fields(result)
    assert section_fields(result, "advanced") == ["reply_timeout_s", "first_reply_timeout_s", "connect_timeout_s", "age_intervals"]
    assert default_of(result, "energy_interval_s") == 5 and default_of(result, "more_groups", "protection") is False

    form = {
        "live_interval_s": 7,
        "energy_interval_s": 2,
        "settings_interval_s": 60,
        "quiet_time_s": 3,
        "live_mode": {"live_mode_live_s": 2, "live_mode_energy_s": 20, "live_auto_off_s": 15},
        "more_groups": {"power32": False, "protection": True, "protection_interval_s": 12, "undocumented": False, "undocumented_interval_s": 60},
        "advanced": {"reply_timeout_s": 2.5, "first_reply_timeout_s": 8, "connect_timeout_s": 4, "age_intervals": 4},
    }
    result = await options.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    # Stored in seconds; the entry reloaded with them, the power32 group on the live group's intervals.
    assert (config.options["energy_interval_s"], config.options["protection_interval_s"], config.options["live_auto_off_s"]) == (120, 43200, 900)
    device = config.runtime_data
    inverter, client = device.inverter, device.poller.client_settings
    assert (inverter.intervals["live"], inverter.intervals["power32"], inverter.intervals["energy"], inverter.intervals["protection"]) == (7, 7, 120, 43200)
    assert (inverter.live_intervals["live"], inverter.live_intervals["power32"], inverter.live_intervals["energy"]) == (2, 2, 20)
    assert inverter.enabled["protection"] and not inverter.enabled["power32"] and inverter.age_intervals == 4
    assert client is not None and (client.quiet_time_s, client.reply_timeout_s, client.first_reply_timeout_s, client.connect_timeout_s) == (3, 2.5, 8, 4)
    # The protection group's entities are there now...
    registry = er.async_get(hass)
    connect_time = f"{config.entry_id}_connect_time"
    await wait_until(lambda: registry.async_get_entity_id("sensor", DOMAIN, connect_time) is not None)

    # ...and go when the group is turned off; the other settings stay, and the form shows them.
    result = await options.async_init(config.entry_id)
    result = await options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    assert default_of(result, "live_interval_s") == 7 and default_of(result, "more_groups", "protection") is True
    form["more_groups"]["protection"] = False
    result = await options.async_configure(result["flow_id"], form)
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("sensor", DOMAIN, connect_time) is None and config.options["live_interval_s"] == 7


async def test_the_cloud_relays_settings_reach_its_blocks_and_a_hidden_setting_keeps_its_value(hass: HomeAssistant, socket_enabled: None) -> None:
    config = entry(server=free_port(), cloud={CONF_CLOUD_HOST: "127.0.0.1", CONF_CLOUD_PORT: 20146})
    await set_up(hass, config)
    # From when TCP polling was on: not shown now, kept.
    hass.config_entries.async_update_entry(config, options={"quiet_time_s": 3.0})
    options = hass.config_entries.options
    result = await options.async_init(config.entry_id)
    result = await options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    assert "quiet_time_s" not in fields(result) and "forward_writes" in fields(result)
    assert section_fields(result, "advanced") == ["reply_timeout_s", "age_intervals", "handshake", "forward_bad_crc"]

    form = {
        "live_interval_s": 30,
        "energy_interval_s": 5,
        "settings_interval_s": 60,
        "forward_writes": True,
        "live_mode": {"live_mode_live_s": 5, "live_mode_energy_s": 60, "live_auto_off_s": 10},
        "more_groups": {"power32": True, "protection": False, "protection_interval_s": 24, "undocumented": False, "undocumented_interval_s": 60},
        "advanced": {"reply_timeout_s": 2, "age_intervals": 3, "handshake": False, "forward_bad_crc": True},
    }
    result = await options.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    poller = config.runtime_data.poller
    cloud = poller.cloud_settings
    assert cloud is not None and (cloud.forward_unknown_writes, cloud.forward_bad_crc) == (True, True)
    assert poller.send_handshake is False and poller.listener is not None and poller.listener.reply_timeout_s == 2
    assert poller.inverter.enabled["power32"] and config.options["quiet_time_s"] == 3.0


async def test_the_issues_fix_opens_the_reconfigure(hass: HomeAssistant, module: TcpModule) -> None:
    assert await async_setup_component(hass, "repairs", {})
    config = flow_entry(mac=None, server=True)
    config.add_to_hass(hass)
    # Loaded, as while the issue is up: Repairs finds the integration's fix flow then.
    with patch(SETUP_ENTRY, return_value=True):
        assert await hass.config_entries.async_setup(config.entry_id)
    issue_id = f"no_answer_{config.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        data={"entry_id": config.entry_id},
        severity=ir.IssueSeverity.WARNING,
        translation_key="no_answer",
        translation_placeholders={"title": config.title, "reasons": "a reason"},
    )
    manager = repairs_flow_manager(hass)
    assert manager is not None
    result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    assert result["step_id"] == "confirm" and result["description_placeholders"] == {"title": config.title, "reasons": "a reason"}
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT and result["next_flow"][0] is FlowType.CONFIG_FLOW
    reconfigure_flow = hass.config_entries.flow.async_get(result["next_flow"][1])
    assert reconfigure_flow["context"]["source"] == SOURCE_RECONFIGURE and reconfigure_flow["context"]["entry_id"] == config.entry_id
    assert reconfigure_flow["step_id"] == "overview"


async def test_a_reconfigure_that_makes_the_inverter_answer_removes_the_issue(
    hass: HomeAssistant, module: TcpModule, relay: MockRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(issues, "NO_ANSWER_GRACE_S", 0.1)
    monkeypatch.setattr(config_flow, "AT_PORT", nobody_port())
    dead = {CONF_ENABLED: True, CONF_HOST: "127.0.0.1", CONF_PORT: free_port()}
    config = flow_entry(mac=None, server=False, client=dead)
    await set_up(hass, config)
    issue_id = f"no_answer_{config.entry_id}"
    await wait_until(lambda: ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None)

    result = await pick(hass, await pick(hass, await reconfigure(hass, config), "no_socket_b"), "client_settings")
    result = await submit(hass, await pick(hass, result, "client"), {CONF_HOST: "127.0.0.1", CONF_PORT: relay.port})
    # The test passed: Finish saves.
    result = await pick(hass, result, "finish")
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    await wait_until(lambda: config.runtime_data.poller.serial is not None)
    await asyncio.sleep(0.3)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize(
    ("socket_b", "notice"),
    [
        (("cloud.example", 20146), None),
        ((HA_IP, 20145), f"sends its data to _{HA_IP}:20145_, not to Renac's cloud (_cloud.example:20146_)"),
        (None, "the module did not answer"),
    ],
)
async def test_the_removal_checks_socket_b(
    hass: HomeAssistant, module: TcpModule, identity: Identity, monkeypatch: pytest.MonkeyPatch, socket_b: tuple[str, int] | None, notice: str | None
) -> None:
    if socket_b is None:
        monkeypatch.setattr(removal, "AT_PORT", nobody_port())
    else:
        module.host, module.port = socket_b
    config = flow_entry(mac=identity.mac.replace(":", ""), server=True)
    config.add_to_hass(hass)
    shown = notifications(hass)
    await hass.config_entries.async_remove(config.entry_id)
    await hass.async_block_till_done()
    removed = [message for key, message in shown.items() if key == f"{DOMAIN}_{config.entry_id}_removed"]
    if notice is None:
        assert removed == []
    else:
        assert len(removed) == 1 and notice in removed[0] and "Set cloud URL with _cloud.example:20146_" in removed[0]
