# Design

The decisions behind the integration and the reasons for them, for whoever
changes it. What the code does in detail is in its docstrings; the protocol
is in [PROTOCOL.md](PROTOCOL.md) and the register table in
[N3-HV-MODBUS-PROTOCOL-V1.00.md](N3-HV-MODBUS-PROTOCOL-V1.00.md); the user's
side is the [README](../README.md); how to build and test,
[DEVELOPING.md](DEVELOPING.md).

## The shape

- **One entry is one inverter device**, with two ways to its data. In
  **server mode** (the cloud relay in the UI) the inverter's module dials
  Home Assistant on port 20145, its socket B pointed at HA by the module's
  own AT setting or by a redirect in the router; HA is the Modbus master on
  that connection and relays the real cloud. In **client mode** (TCP
  polling) HA dials a TCP endpoint and polls through it, in practice the
  w2b relay's port bridged to the module's Bluetooth; HA does not care what
  is behind the port. Both can be on: server mode first, the client while
  the module is not connected.
- **The cloud keeps working.** With the cloud relay HA answers the cloud's
  reads from its own state where it can, forwards the rest, passes the
  cloud's clock writes and holds its other writes back by default. The
  reason is the Renac app: it reads the cloud, and owners keep using it. In
  client mode HA does not touch the cloud at all; it tolerates the cloud's
  traffic on the shared UART instead.
- **HA is read-only towards the inverter.** There is no write path to the
  inverter anywhere. Forwarding a cloud write is not HA writing. HA's one
  write is to the module's configuration, its socket B, always opt-in and
  behind a warning that says it changes a setting in the module's firmware.
- **No pymodbus.** Its RTU framer would not survive the module's leading
  `FF`/`00` padding, and it cannot be the accepting side that server mode
  needs. The core is pure asyncio on the standard library.
- **One asyncio task per connection, no threads, no polling loops.** The
  integration was written for a small arm64 box running HA in Docker; it
  must cost little CPU and memory (about 3 MB of code and 1 MB per
  inverter, measured).
- **The core is standalone** (`renac_protocol/`): standard library only, no
  Home Assistant import, enforced by a test. It lives inside the
  integration folder because HACS installs that folder only. Imports flow
  one way: poller → inverter, connection, listener, cloud session → channel
  → framer → frames → crc; inverter → groups → register table → registers.
  Defaults, ports and timeouts live in the core's `const.py`.
- **No unique id on the entry.** Neither way in can read the module's MAC,
  and nothing needs one: the device and its entities are keyed by the
  entry's id, the inverter's serial is checked on every connection, and a
  second entry for the same inverter is allowed (a real one and a mock side
  by side). The module's MAC and IP from the setup are kept as information.

## The core's blocks

Small blocks, each with one job, driven from outside; whatever lives as
long as a TCP session is made with the session and dropped with it. Nothing
installs a callback on a block it does not own: frames are pulled from a
channel, a listener hands each session to its one owner, logging is the
trace.

| Block | Job |
|---|---|
| `Channel` | one TCP stream and its framer; `frames()` is pulled by its one consumer; every chunk in and out goes to the logger `renac_protocol.raw` at debug level, the hexdump |
| `Connection` | one request at a time with its reply matched; `hold()`; `on_unsolicited` for frames that answer no request |
| `dial()` | client mode's way in: connect, and allow the first reply 15 s while the relay brings its BLE link up |
| `Listener` | server mode's port; one session at a time, a new connection replaces the one before |
| `read_group()` | a group's ranges in order, each a fn 3 read, into a `GroupResult`; the first failing range ends the group |
| `Inverter` | the schedule, the raw words with their times, each group's last result; no I/O; readings decoded on demand |
| `CloudSession` | one per module session: connects to the cloud, answers or forwards its requests, applies the write policy |
| `Poller` | the one loop: what is sent, when, through which connection; server first, the client while the module is away |

**The framer** drops leading `00`/`FF` bytes, knows the frame lengths of
both directions (a device stream carries replies, a master's stream
requests), verifies CRCs, resyncs by one byte on a failure, and discards a
partial frame idle for more than 300 ms. In the cloud's direction a
request-shaped frame with a bad CRC is handed on whole, marked bad, for the
cloud session to drop or forward.

**One request at a time**, every master included. A reply is matched by
shape: fn 3 by `bytecount == 2 × count`, an exception by `fn | 0x80`, fn 16
by the echoed address and count. Reply timeout 3 s (the app uses 6 s; the
observed median is 56 ms, the maximum 538 ms), one retry, then the group's
read fails; at least 100 ms between requests (the cloud uses about 150 ms).
A frame that matches nothing is counted, logged at debug level and handed
to the owner; the connection keeps waiting for its own reply. In client
mode an unsolicited frame means the cloud is polling over the shared UART,
so the client-mode code holds the next request back for the **quiet time**,
20 s by default; server mode never holds. Two priorities: a request with
the high priority, the cloud's in server mode, is the next one sent, after
the request in flight; a cloud request is sent once and never repeated
after a timeout, since a repeated write would reach the inverter twice.

## Poll groups and the schedule

| Group | Registers | Default | Live mode |
|---|---|---|---|
| identity | 10000×32 (model, serial), 10176×6 (versions), 10032×16, 10048×80, 10128×48 (BMS serials, not decoded; read in the cloud's shapes so the state can answer its reads) | at connect, then daily | — |
| live | 11000×114 | 30 s | 5 s |
| power32 (off by default) | 11124×34, the undocumented 32-bit copies of the battery, meter and consumption powers, under the live group's keys: its readings stand in for the 16-bit ones while its last read succeeded | 30 s | 5 s |
| energy | 14000×27 | 5 min | 60 s |
| settings | 20000×1, 20001×6 (the clock), 20007×7, 20100×14, 21000×1, 21016×6 | 1 h | — |
| protection (off by default) | 22000×9, 22100×11, 22200×10, 23000×11, 23100×10, 23200×26, 23300×17, 23400×2 | 24 h | — |
| undocumented (off by default) | 20114×1, 20130×1, 50028×1, 50029×1, 50030×1 | 1 h | — |

One schedule per inverter, whichever way the updates go. A client update:
dial, the identity read, the due groups in the order identity, settings,
live, power32, energy, then the groups that became due meanwhile, each once
per update, then close, unless live mode holds the connection. In server
mode the module's session is permanent: the identity read at its start,
then the groups on their timers.

## The poller's loop

- **A module session.** The listener hands the poller a connection; the
  loop takes it at its next step, so a client update under way finishes
  first. The cloud session is made at once. After a settle time of 1 s (the
  module sends nothing until asked) the poller reads `10000 × 32` and
  compares the serial with the learned one. Ours, or none learned yet: the
  serial is learned when none was, the session is checked, the cloud
  session answers from the inverter from now on, and the handshake goes
  when no cloud is connected. Another inverter's: the session is refused,
  nothing stored, the reason kept for the diagnostics; it stays open, idle,
  its cloud served without answers from the inverter. No reply: the check
  runs again in 10 s.
- **The source of an update** is the server while its session is checked,
  the client while no session is open or the open one is refused, none
  otherwise. With no source, once there was one, the due groups fail with
  "no connection" and their entities go unavailable; before the first
  source nothing fails. A session just checked reads the groups that failed
  at once, not after their retry wait. A client update that meets another
  inverter's serial is closed, the reason kept, and the next update checks
  again. A lost or replaced connection ends an update; the groups not read
  stay due.
- **The handshake when no cloud is there.** While the cloud is not
  connected the poller sends the cloud's handshake itself, once per
  session, because it gives the module's identity (serial, MAC, firmware,
  IP); neither Bluetooth nor Modbus over socket B needs it, and an option
  leaves it out. No keepalive of its own: the polls keep the session busy.
- **The cloud.** A name is resolved by the core's own DNS client asking a
  server directly, `8.8.8.8` by default, because the LAN's DNS may be
  hijacked by the router's own redirect; the system resolver is an option,
  and a caller can supply its own way to open the connection (the tool's
  SOCKS5 proxy). Cloud down: the module session and the polling go on, the
  cloud is retried after 5, 10, 30, then every 60 s, and 5 s after a drop.
  The module reconnects to HA by itself after an HA restart, on its own
  socket timeout.
- **The words.** The raw word of every register read, by address, with the
  time it was last read or written. A register is valid while its age is
  within its limit: the register's own when the table gives one, else its
  group's, three of the group's intervals by default (a setting). The
  clock's limits follow its units: seconds 5 s, minutes 1 min, hours 1 h,
  the date 1 day. A failed read changes nothing, the words just age. The
  inverter's exceptions are never kept: a block this unit refuses may work
  on another model.
- **The cloud's reads.** A fn 3 request for registers that are all in the
  words and valid is answered at once, with a reply built from the words,
  CRC included. Any other request goes to the module as the next request,
  and its reply back to the cloud. The cloud's requests keep their own
  order.
- **The cloud's writes.** A write within the clock, 20001–20006, always goes
  to the inverter; its echo returns to the cloud and the words take the
  written values with the time. Any other write is not forwarded by
  default: no answer to the cloud (it times out as it does on its own bad
  frame, and the app shows the change failed), a warning with address and
  values, a line in HA's notification and an event. A setting forwards
  them; a forwarded write is logged at info level and reported as an event
  only. A cloud frame with a bad CRC (the cloud's permanent one) is dropped
  by default and logged once per cloud connection; a setting forwards it as
  received.
- Module output nobody asked for, a phone app's replies broadcast onto
  socket B, goes to the cloud unchanged. The cloud's AA55 frames go to the
  module in the cloud's turn and the module answers them itself.

## Entities

- **The device**: one per entry, identifiers `(renac_local, <entry id>)`,
  named after the entry until the model is read, then after the model;
  manufacturer Renac, the serial, `sw_version` from 10176–10178. Nothing
  else on the device: versions are diagnostic sensors, the module's
  identity is in the diagnostics download.
- **Entities** come once the inverter's serial is known: at setup when the
  entry holds it, else at the first identity read. Until then the entry has
  none, and after 60 s without an answer a fixable Repairs issue says why;
  its fix opens the entry's reconfigure. Unique id `<entry id>_<key>`,
  entity id `<domain>.renac_inverter_<NNN>_<key>` with `NNN` the serial's
  last three digits; HA keeps an id once registered.
- **An entity is unavailable only while its group's last read failed** and
  keeps its value otherwise: a module session that ends changes nothing
  until the group is due; then the client reads it, or, with no way in, the
  group fails.
- **Names** come from the register table, in English, not translated; each
  entity's translation key is its register key, for `icons.json` and the
  dashboard, not for a name. No name says "total": a sum of phases or
  strings and a lifetime counter have the plain name (Meter 1 power, Grid
  import energy), today's counter ends in "today", and the keys are the
  same words. The unit gives the device class; a live number is a
  measurement, an energy counter `total_increasing`; the table's diagnostic
  flag makes a diagnostic entity, its off-by-default flag a disabled one.
- **Enums** are HA's enum sensors. The state is the table's name as a key,
  lowercase with underscores (HA's `slugify`), because hassfest wants the
  icons' and translations' state keys so; the names themselves are the
  states' translations in `strings.json`. A value the table has no name for
  is unknown.
- **Three-phase quantities.** HA has no entity that holds other entities,
  so a quantity whose sum means something (inverter power, Meter 1 power,
  consumption power, backup power) is one enabled sensor, the sum, with the
  phases as attributes `l1`, `l2`, `l3`, its per-phase sensors disabled by
  default; a quantity whose sum means nothing stays per phase (grid voltage
  enabled; grid frequency, inverter current and the backup voltage,
  current and frequency disabled). PV power is PV1 plus PV2 with the
  strings as attributes. Meter 2 and 3 are disabled by default; the entity
  registry keeps what the user enables.
- **Shown as one**: the sixteen fault words as one Faults sensor, the set
  bits by name, `none` when clear, the non-zero words as attributes; the
  six clock registers as one Clock sensor in HA's time zone; the battery
  status as its mode with the request flags as attributes; BMS and Meter 1
  connected and the charging and discharging bans as binary sensors.
- **The ways in as entities**, always present, the dashboard's badges:
  Cloud relay `off`, `waiting` (no module connected), `checking` (identity
  not checked yet), `active`, `refused` (another inverter's serial); TCP
  polling `off`, `standby` (the poller reads the other way), `active`,
  `failing` (the last update failed as a connection), `refused`; and Cloud,
  a binary sensor, when a cloud is configured. The device's "connected via"
  is not used for the way in: it names another device, a fixed link, not a
  state.
- **The w2b relay's battery**: with client mode, when the address answers
  `http://<address>/api/status` with `device.battery_mv` at setup, two
  diagnostic sensors read every 5 minutes by a coordinator of their own, a
  percentage by the curve of the relay's own page and the voltage;
  unavailable while the page does not answer, unknown while the relay
  reports no battery. Any other answer: no relay battery, nothing shown.
- **Diagnostics download**: the entry's data; the poller's state, the
  refusals and the module's info; the last connection with its counters;
  the cloud's status, counters and last write; each group's last result;
  the readings as text. Only the inverter's serial is redacted; nothing
  else in it names the unit or its owner.
- **The cloud's writes outside the clock**: an event
  `renac_local_cloud_write` per write, blocked or forwarded, with the entry
  id, address, count, values and whether it was forwarded. A blocked one
  is also a line in the entry's persistent notification, every blocked
  write since the notification was last dismissed; past 100 lines the
  earlier ones are counted; an HA restart empties it.

## Live mode and the dashboard

**Live mode** is a switch per inverter: on, the live and power32 groups
read every 5 s and energy every 60 s, and the client's connection is held
between updates; it turns itself off after 1 minute by default, a look at
the live values as the app's local screen gives. The device holds the
auto-off timer; a reload starts with it off. Recorder load at 5 s × 15
entities × 1 min is about 180 rows per look.

**The Renac dashboard.** HA has no public call for an integration to add a
dashboard, so the integration puts a storage dashboard, `renac-local`,
into lovelace's dashboards and registers its sidebar panel; two of
lovelace's internals are used, its data and its storage class, so a change
in them costs the dashboard, not the integration. The views are plain
stored views built in Python from the registries, a tab per inverter with
a live subview and an Energy tab of HA's energy cards. No script of the
integration's in the page: a script an integration registers reaches only
pages loaded after its setup, and every page loaded while HA starts would
lack it. No card of our own. The views are written at setup, a second
after a change of the integration's entities or devices in the registries,
and at an entry's removal, while the stored dashboard is still what the
integration wrote last, which its own store `renac_local.dashboard`
remembers; the user's first edit ends that, and Configure → Rebuild writes
the views over it again. The last entry's removal takes the sidebar item
and deletes the stored views when they are the integration's; edited ones
stay. The day's graphs are statistics graphs of the recorder's 5-minute
means, because the raw states, read every few seconds, draw as steps with
gaps in HA's history graph. The Energy tab needs HA's energy dashboard set
up and is the whole home's, not one inverter's.

## The setup flow

HA's flow forms have editable fields and one button, and a flow has no way
back, so: a value only shown goes in the step's text; a choice is a menu
step whose options act as buttons; where a form needs a way back, the menu
after it has Back, with what was typed. Every form's button says Next. The
texts are simple English (A2–B1), short, with an example wherever a value
is typed, lists rather than paragraphs; a URL goes in as a placeholder,
since HA's validator refuses URLs in translations. The README's diagrams
are in the forms too, as Markdown images loaded from the repository on
GitHub by a placeholder URL (`IMAGES_URL` in `config_flow.py`): a relative
path resolves against the page, and HA serves nothing of an integration's
folder without the `http` dependency, not worth it for four pictures; the
texts around them do not depend on them. HA's advanced mode is not used
(deprecated in 2026.9): what it would hide is in a collapsed section. Every
step that changes socket B says it overwrites a setting in the module's
firmware.

Facts of the flow worth keeping:

- The module is found by the AT discovery, a broadcast on UDP 48899, three
  tries of 2 s; modules often miss a search. A typed address is probed
  with the greeting alone. A module another entry has is listed as already
  added, but can still be typed in.
- Socket B's value is kept as the real cloud endpoint only when none is
  kept yet: after the module is pointed at HA, socket B names HA, and a
  kept endpoint is never overwritten.
- The listen port is 20145 or the first after it that no other entry's
  server mode uses; another entry's port is refused. HA's address toward
  the module is socket B's host when it names HA, else the host of HA's
  local network URL when that is a name, else `homeassistant.local`; the
  module resolves a name through its DNS server, usually the router, not
  mDNS.
- Before the module's setting changes, HA opens the listen port (or
  watches the entry's running listener), so the module's first dial
  counts; then it waits up to 30 s for a connection from the module's IP.
  The revert option sets socket B back to its value before and is itself
  state-changing.
- TCP polling's test reads `10000 × 32`; a serial read there is stored as
  the learned one.
- The entry is titled `Renac <module IP>` and renamed `Renac <model>
  <serial>` once both are read, only while its title is the one the
  integration gave last (`given_title`); a title the user typed stays.
- The entry's data: `module` (`host`, `mac`), `server` (`enabled`, `bind`,
  `port`, `cloud_host`, `cloud_port`, `dns_server`), `client` (`enabled`,
  `host`, `port`), `serial`, `given_title`. The settings live in the
  entry's options, times in seconds; a setting the options lack is the
  core's default, so an entry from before runs as before. Save reloads the
  entry.
- Removal leaves socket B as it is: HA's delete dialog takes no text from
  an integration. After the deletion, when the entry knew the module, HA
  reads socket B once more and notifies when it is not the kept cloud
  endpoint, with how to set it back.
- **The AT session** (`at_session.py`) holds three things and nothing
  else: the discovery, the greeting with the `AT+SOCKB` query, and the
  `AT+SOCKB` set, logged at warning level with the value before and after.
  On this firmware any AT command that waits for something hangs the
  module's network side until a power cycle, so nothing else is ever sent
  (PROTOCOL.md, the AT side).

## Tests

A test checks logic that is used, once, at the lowest level that shows it;
never a constant, a default or a table's values; code only the tests use
is removed with its tests. The tests hold no data of the real unit at any
time: identities and values are random, made by the test itself or by the
fake module, so no capture is needed. The core's tests run anywhere; the
HA glue's run in Docker in the official HA image of the targeted release
with that release's test harness. The core's tests run there with the
harness's plugin off (`-p no:homeassistant`): its pytest-socket blocks
every socket, 127.0.0.1 included. The glue tests send the AT discovery to
the fake's port on 127.0.0.1, never a broadcast.

## Known limits

- In client mode the cloud may take one of HA's replies as its own when the
  shapes match: the same registers, harmless, and what the phone app does
  today; the quiet time keeps overlaps rare.
- The cloud's IP may change while the LAN's DNS is hijacked: the DNS server
  is a setting, and resolution failures are logged clearly.
- A blocked cloud write gets no answer: the cloud times out, the app shows
  the change failed, HA reports it.
- Only one central can hold the module's Bluetooth: while the relay holds
  the link the Renac app cannot connect over Bluetooth.

## Defaults

| | |
|---|---|
| Server mode | port 20145, bind `0.0.0.0`; the cloud `germany.le-pv.com:20145`, DNS `8.8.8.8` |
| Client mode | port 8899; connect timeout 10 s, first-reply timeout 15 s, quiet time 20 s |
| Requests | reply timeout 3 s, one retry, 100 ms gap; settle time 1 s; a session's identity check retried after 10 s |
| Groups | live 30 s, energy 5 min, settings 1 h, protection 24 h, identity daily; a failed group retried after 60 s; the words valid for three intervals |
| Live mode | live 5 s, energy 60 s, auto-off 1 min |
| Cloud | retries 5, 10, 30, then 60 s; 5 s after a drop |
| Relay battery | every 5 min, request timeout 10 s |
