# Developing

How the repository is laid out, how the tests run, and the command-line
tools that read an inverter without Home Assistant. The user's guide is
the [README](../README.md).

## Layout

```
custom_components/renac_local/    the integration: manifest, config flow, the inverter device, entities and their
                                  icons, the Repairs issue and its fix, the removal's check, the Renac dashboard
                                  and its views
  renac_protocol/                 the standalone core in small blocks: the frames and the framer, the channel, the
                                  connection and the listener, the poll groups and the register table, the inverter,
                                  the handshake, the DNS client, the cloud session, and the poller that runs them;
                                  the module's AT session
tools/                            relay_client.py (client mode), cloud_relay.py (server mode) and set_sockb.py (the
                                  module's socket B), the command-line tools
tests/                            pytest: core tests, mocks on 127.0.0.1, file tests
  ha/                             the Home Assistant glue's tests, their Docker image, a test HA instance
fixtures/                         ha_config/, the test HA instance's configuration
docs/                             this guide, the protocol as recovered, the vendor register table and what the
                                  inverter answers, the README's images
```

`renac_protocol` is standard library only and imports nothing from Home
Assistant; `tests/test_protocol_standalone.py` enforces it. The tests and
the tools import it by its bare name, the integration relatively.

The two ways in are called server mode and client mode in the code, the
cloud relay and TCP polling in the user interface. In server mode the
module dials Home Assistant, which polls the inverter on that connection
and relays the real cloud. In client mode Home Assistant dials a TCP
endpoint and polls through it, the w2b relay's port in practice. With
both, server mode comes first and the client polls while the module is
not connected. Every connection first reads the inverter's serial: the
first one is kept, and a connection with another one is not polled.

## Development

Python 3.12 or later in a venv, activated for the commands below.
`requirements_test.txt` holds everything, `bless` for the
fake module below included; it comes from bless's GitHub master because
the PyPI release pins WinRT packages that its own bleak refuses on 3.12
and later, so git has to be on the PATH.

```
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements_test.txt
python -m pytest
python -m ruff check .
```

The DNS client's tests ask Google's DNS `8.8.8.8` over the internet;
`-m "not network"` leaves them out.

The core tests and the tools run on any platform. The Home Assistant
glue's tests, `tests/ha`, need Home Assistant, which runs on Linux only;
where it is not installed, pytest leaves that folder out. They run in
Docker (Linux or WSL2), in the official Home Assistant image of the
release the integration targets with the test harness of that release.
`tests/ha/test.sh` builds the image when it is missing or its Dockerfile
changed, then runs the tests in it, the core's on the image's Python
3.14 included; the container is removed after the run. Its arguments are
pytest's, paths from the repo's root:

```
tests/ha/test.sh
tests/ha/test.sh -x -q
tests/ha/test.sh tests/test_poller.py
tests/ha/test.sh tests/ha -k config_flow
```

`tests/ha/compose.yaml` starts a Home Assistant test instance with this
integration, `fixtures/ha_config` its configuration, and the mocks around
it on a Docker network of their own: the mock cloud and two inverters, each
a fake module dialling the cloud, with its AT side and its link in place of
Bluetooth, and a mock relay bridging to that link. How to run it and what
to try: [tests/ha/README.md](../tests/ha/README.md).

The README's diagrams are hand-written SVG files in `docs/images/`, one
shared style: a light card, blocks with coloured outlines, the cloud relay
in blue, TCP polling in purple, warnings in amber.

## Reading the inverter from the command line

`tools/relay_client.py` connects to the relay's rule port, reads the poll
groups once and prints every value with its register and raw words, the
three phases of a value on one row as `L1/L2/L3`. It is read-only: it
sends Modbus function 3 requests and nothing else.

```
python tools\relay_client.py w2b.local 8899
python tools\relay_client.py w2b.local --groups live,energy --period 5 --live
python tools\relay_client.py w2b.local --all --raw
python tools\relay_client.py w2b.local --json
```

Groups: `identity`, `settings`, `live`, `energy` by default; `--all` adds
`power32`, `protection` and `undocumented`; `--full` adds the registers
that are off by default (BMS detail, Meter 2 and 3). `--use-32bit` adds
the `power32` group, the power values from the undocumented 32-bit block;
its own table shows them, and when its read fails its table says so and
the live table's 16-bit values stand. `--period N` repeats every N
seconds, clearing the terminal before each print; Ctrl+C stops. `--live`
keeps the connection between cycles; without it every cycle connects and
disconnects. `--raw` prints every chunk of bytes in and out and every
frame with its decoded form, `--json` prints JSON. `--backoff` is the
quiet time: how long requests are held after a frame nobody asked for,
the cloud's traffic on the shared link (default 20 s, 0 for none). Exit
code 0 when every group read, 1 when a group failed, 2 when the relay
could not be reached.

Without a relay, the mock relay answers on 127.0.0.1, from random words,
so the values mean nothing; it shows the tool working:

```
python -m tests.mocks.mock_relay --port 18899 --unsolicited 10
python tools\relay_client.py 127.0.0.1 18899 --all
```

## Reading the inverter in server mode

`tools/cloud_relay.py` waits for the inverter's module on port 20145, as
the cloud would, polls the inverter, relays the cloud, and shows the
values with the time each group was read, the module's identity from its
handshake and the cloud's state. The core's poller runs it, as in Home
Assistant, clearing what was read when a module session ends; everything
the core logs, every frame included, goes to `cloud_relay.log` in the
current folder (`--log`). Its own requests are function 3 reads, the
identity read first, and the module's handshake. The cloud's reads of
registers the polls read recently enough (three of the group's
intervals; the clock's seconds 5 s) are answered from those values; its
other requests go to the module as the next request, once, and the
replies back. Its clock writes (20001–20006) reach the inverter; its
other writes are blocked, logged as warnings, and get no answer;
`--forward-unknown-writes` passes them (**state-changing**: they then
reach the inverter). Its frames with a bad CRC are dropped, logged once
per cloud connection; `--forward-bad-crc` passes them as received.

```
python tools\cloud_relay.py
python tools\cloud_relay.py --cloud germany.le-pv.com
python tools\cloud_relay.py --cloud germany.le-pv.com --proxy 127.0.0.1:1080
python tools\cloud_relay.py --proxy 127.0.0.1:1080 --check-proxy
python tools\cloud_relay.py --bind 127.0.0.1 --groups live,energy --live
```

`--bind` and `--port` (default `0.0.0.0:20145`); `--cloud NAME[:PORT]`,
resolved through `--dns` (default `8.8.8.8`, `system` for the system's
resolver), or `--cloud-ip ADDRESS[:PORT]`; no cloud by default. The cloud
connection opens when the module connects and is tried again after 5,
10, 30, then every 60 s. `--proxy IP:PORT` sends it through a
SOCKS5 proxy without authentication, the proxy resolving the cloud's
name; the module's connection stays direct. `--check-proxy` prints the
address `api.ipify.org` sees, directly and through the proxy, and exits
(code 1 when the proxy failed). While no cloud is connected the server
sends the module the cloud's handshake itself, 1 s after the module
connects; `--no-handshake` leaves it out. The groups as
`relay_client.py` takes them, `identity`, `settings`, `live`, `energy` by
default; `--live` for live mode. Exit code 0, or 2 when the port cannot
be opened.

The real module dials it once the router sends its cloud connection to
this computer, or once its socket B is set to it. Without it, the fake
module below dials it:

```
python tools\cloud_relay.py --bind 127.0.0.1
python -m tests.mocks.mock_hf_lpt270 --tcp 127.0.0.1:20145
```

## The module's socket B

`tools/set_sockb.py` reads the module's socket B, the address its cloud
connection dials, over the module's AT side on UDP 48899, or sets it. A
set is **state-changing**: the module dials the new address at once. It
sends the AT greeting, `AT+SOCKB` and `AT+Q`, nothing else; a set's value
is checked before it is sent, and a value socket B already holds sends
nothing. Any other AT command is out of bounds: on this firmware a wrong
one hangs the module's network side until the inverter is power cycled.

```
python tools\set_sockb.py
python tools\set_sockb.py 192.168.1.20:20145
python tools\set_sockb.py --reset --module 192.168.1.10
```

The module is the one a broadcast finds, unless `--module IP`; when none
or more than one answers, it lists them and stops. `HOST:PORT` is an IPv4
address or a name the module's DNS resolves; `--reset` sets Renac's
`germany.le-pv.com:20145`. Exit code 0, or 1 when nothing was read or set.

## A fake inverter module on this computer's Bluetooth

`tests/mocks/mock_hf_lpt270.py` advertises the module's service `FEE7`
with `FED5` (write) and `FED6` (indicate) and answers like an N3-HV whose
values move: the sun rises and sets over a day of 24 hours (`--day
SECONDS` for a shorter one), the house's consumption wanders, the meter
and the energy counters follow. It logs the requests it answers and who
subscribes; `-v` adds every write and indication as hex.
Its identity is Renac's demo plant's, model `N3-HV-10.0`, inverter
`8301033231001064`, module `2200000C123C4121`; `USE_RANDOM_SN_INV` and
`USE_RANDOM_SN_MOD`, in `demo_identity()` near the top of the file, give
the inverter's and the module's serial a random tail, a new one every
run, logged when it starts. `--model` (`N3-HV-5.0`, `-6.0`, `-8.0` or
`-10.0`, its rated power with it) and `--serial` (16 letters or digits)
give another inverter. Its MAC is made from the module's serial. Its
energy counters start from random totals, and its clock starts
on UTC+8, the demo plant's time zone, and runs on from the last write to
it.
With `--tcp HOST:PORT` it is the module of server mode instead, without
Bluetooth: it dials the server, answers the handshake besides Modbus and
dials again 5 s after a drop; its AT side answers on UDP `--at-port`
(48899), a broadcast too: the greeting, with its own address,
`AT+SOCKB`, whose set moves socket B at once, and `AT+Q`. With `--link PORT` it listens on TCP in place of Bluetooth, for
`tests.mocks.mock_relay --module 127.0.0.1:PORT`. From this repo's root:

```
python -m tests.mocks.mock_hf_lpt270
python -m tests.mocks.mock_hf_lpt270 -v
```

On Windows the advertisement carries the computer's name, not the mock's.

Then make a relay rule to this computer from the relay's page (scan, pick
it; the service and both characteristics resolve by Auto) and watch the
values move:

```
python tools\relay_client.py w2b.local 8899 --groups live,energy --period 5 --live
```
