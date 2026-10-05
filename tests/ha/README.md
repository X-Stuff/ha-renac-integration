# The Home Assistant test instance

Home Assistant with this integration and the mocks around it, from
`tests/ha/compose.yaml`: two inverters, each a fake module with a mock
relay, and the mock cloud, on a Docker network of their own,
`172.31.250.0/24`. Linux or WSL2 with Docker; the commands run from the
repo's root. Nothing in it reaches Renac's cloud or a real module.

| Service | Address | What it is | Ports |
|---|---|---|---|
| `homeassistant` | .10 | Home Assistant 2026.9.2, the configuration `fixtures/ha_config`, the integration mounted read-only | 8123, also on the host; 20145 for server mode |
| `mock-cloud` | .20 | Renac's cloud as the mock plays it: polls whoever dials it | 20146 |
| `mock-module-1` | .31 | the fake HF-LPT270 of an N3-HV-10.0, serial `8301033231001064`: socket B dials the mock cloud; its AT side; a TCP link in place of Bluetooth | UDP 48899, 18898 |
| `mock-module-2` | .32 | the same, an N3-HV-6.0, serial `8301063231000206` | UDP 48899, 18898 |
| `mock-relay-1` | .41 | the BLE relay's stand-in, bridging its clients to module 1's link | 8899 |
| `mock-relay-2` | .42 | the same, to module 2's link | 8899 |

Only Home Assistant's port is published on the host. A broadcast stays on
the network: a search for modules there finds the two fake ones and
nothing else. When `172.31.250.0/24` overlaps a network of your host's,
change it in `compose.yaml`, every address with it.

## Start

```
docker build -t renac-ha-test:2026.9.2 tests/ha     # once: the image the mocks run in (tests/ha/test.sh builds it too)
docker compose -f tests/ha/compose.yaml up -d
```

Open http://localhost:8123 (from Windows too: WSL forwards the port) and
log in as **admin**, password **admin**. The user and the finished
onboarding come from `fixtures/ha_config/.storage`, copied into the
instance's state when it has none yet.

## Add an inverter

Settings → Devices & services → Add integration → **Renac local**:

1. Next, and the search lists both fake modules, `172.31.250.31` and
   `.32`; choose one. "Other IP address" with an address where no module
   answers, `172.31.250.99`, shows the way to TCP polling only.
2. Its socket B, the mock cloud `172.31.250.20:20146`, is read and kept
   as the cloud.
3. Choose the ways: Cloud relay, TCP polling, Both, or Set up later.
4. The cloud relay: port 20145 (20146 for the second inverter), and Home
   Assistant's address: keep `homeassistant.local`, which Docker's DNS
   answers on this network as a router that knows the name would (the
   network has no mDNS), or type `172.31.250.10`. Then change the module's
   setting: the module dials Home Assistant at once, which ends the wait
   for it; Home Assistant relays the mock cloud. A name nobody answers,
   `nobody.local`, shows the menu after 30 s: wait again, revert and start
   over (back to the mock cloud), or keep it.
5. TCP polling: the module's relay, `172.31.250.41` (or `.42`), port
   `8899`; Next tests it and shows the model and the serial. A wrong port
   shows the error; Back fixes it.

The device appears as the fake module's model, and the entry, `Renac
172.31.250.31` at first, is renamed `Renac N3-HV-10.0 8301033231001064`
at the first read. Its entities appear once the inverter's serial is
read, as `sensor.renac_inverter_064_*`: the last three digits of the
serial; the second inverter's are
`sensor.renac_inverter_206_*`. An inverter that does not answer within a
minute of the setup gets a Settings → Repairs issue saying why; it goes at
the first answer.

A fake module's socket B lives only as long as its process: after a
`restart` or `down` of it, it dials the mock cloud again, and an entry
that pointed it at Home Assistant waits for it until it is pointed there
again (`tools/set_sockb.py`, below).

## Things to try

- **An inverter that never answers:** add an entry with TCP polling only,
  port `8898`, where nothing listens, and Ignore and finish after the
  failed test: no entities, and after a minute the Repairs issue with the
  client's error. The issue's Next opens the entry's reconfigure: TCP
  polling, Change the settings, port `8899`, then Finish.
- **Reconfigure:** the entry's ⋮ menu → Reconfigure reads the module and
  shows the entry's menu: each way's settings, turning one off, Set cloud
  URL, Forget the serial number; each one saves when it is done. "Turn
  off the cloud relay" while the module sends to Home Assistant offers to
  set it back to the mock cloud first.
- **Set up later:** the entry has its device and no entities; Reconfigure
  sets a way up.
- **Set cloud URL:** the entry's Configure → Set cloud URL; the field holds
  the module's socket B, `172.31.250.20:20146` points it back at the mock
  cloud. An entry whose module never answered (`172.31.250.99`) has only
  Settings in that menu.
- **Settings:** Configure → Settings; set Live data to 10 s, turn on the
  32-bit powers and Protection settings, Save: the entry reloads, the live
  entities update every 10 s, the protection entities appear under
  Diagnostic, and turning them off again removes them. With the cloud
  relay on, "pass the cloud's writes" lets the mock cloud's write outside
  the clock through: an event, no notification; off, the notification
  lists it.
- **Live mode:** the device's Live mode switch. On, the live entities
  update every 5 s, TCP polling stays `active` on one held connection, and
  the attribute `turns_off_at` says when it ends, a minute later by
  default (Settings → Live mode changes it). It turns itself off then, and
  the updates go back to every 30 s.
- **Removal:** delete an entry whose module sends to Home Assistant: a
  notification says so, with the mock cloud's address. Set cloud URL back
  to the mock cloud first, and the deletion leaves none.
- **The relay away:** `docker compose -f tests/ha/compose.yaml stop mock-relay-1`.
  Within about 30 s the live entities are unavailable; energy and settings
  keep their values until their group is due. `start mock-relay-1` brings
  them back at the next update.
- **The modules' socket B:** `tools/set_sockb.py` run in a container on the
  network, for example the mock cloud's:
  `docker compose -f tests/ha/compose.yaml exec mock-cloud python3 tools/set_sockb.py`
  searches and lists both modules; with `--module 172.31.250.32` it reads
  module 2's socket B, and with `172.31.250.10:20145 --module 172.31.250.32`
  points it at Home Assistant (`172.31.250.20:20146` points it back at the
  mock cloud). The module's log shows it moving.
- **Diagnostics:** the device's ⋮ menu → Download diagnostics.
- **Logs:** `docker compose -f tests/ha/compose.yaml logs -f homeassistant`,
  the integration at debug level, every frame; the same with a relay, a
  module or `mock-cloud`.
- **Changed code:** `docker compose -f tests/ha/compose.yaml restart homeassistant`
  loads it; the integration's folder is mounted, not copied. The mocks
  take changed code at `restart` too.
- **The REST API:** make a token in your profile (Security → Long-lived
  access tokens), then for example
  `curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8123/api/states/sensor.renac_inverter_064_pv1_voltage`.

## Stop

```
docker compose -f tests/ha/compose.yaml down      # the containers go; the instance's state stays for the next up
docker compose -f tests/ha/compose.yaml down -v   # the state goes too: the next up starts from admin/admin, no entries
```
