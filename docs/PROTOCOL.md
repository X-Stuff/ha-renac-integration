# Renac local protocol, recovered from `app.renac.ess` 5.5.64

Two sources, cross-checked against each other:

1. The decompiled classes under
   `app/src/main/java/app/renac/ess/local/` (not in this repository) —
   `BlueComm`, `Bluttoothlocal`, `RenacStorage`, `PubCal`.
2. [RENAC N3 HV Modbus Protocol V1.00](./N3-HV-MODBUS-PROTOCOL-V1.00.md),
   the official register table.

Nothing here is guessed. Every frame below is a byte array that is hard-coded in
the APK, and every CRC in those arrays has been verified against the CRC routine
that ships in the same class. Every register has been checked against the
official table; **section 8 lists the three places where the app and the
document disagree**, and what this repo does about each.

---

## 1. Headline finding

**The app speaks plain Modbus RTU. There is no envelope, no handshake, no
authentication, no vendor wrapper.**

`BlueComm.WriteData()` passes the byte array straight to
`BluetoothGattCharacteristic.setValue()` and writes it. The bytes on the BLE
characteristic are literally `01 03 27 10 00 70 4F 5F`. The HF-LPT270 module is a
transparent bridge: BLE/WiFi in, UART out. So the frames below are exactly what
the inverter's serial port expects, whichever side you come in from.

The wire format was therefore never the problem. **Section 6 settles the TCP
question: socket A on port 8899 is not wired to the UART in either direction,
Renac's own app never opens a TCP socket at all, and no setting on this firmware
can change that.** Use BLE, or RS485 on the inverter's COM pins.

### The link

| | |
|---|---|
| BLE device name | `HF-LPT270` — matched with `String.equals` in `BlueComm.receiver`, over **classic** discovery (`startDiscovery()` + `ACTION_FOUND`), then `connectGatt()` for the actual traffic. It calls `getUuids()` and discards the result, so it never filters on advertised services. |
| GATT service | `0000fee7-0000-1000-8000-00805f9b34fb` |
| Write characteristic | `0000fed5-…` — selected by `getProperties() == 8` (`PROPERTY_WRITE`) |
| Indicate characteristic | `0000fed6-…` — selected by `getProperties() == 32`, which is `PROPERTY_INDICATE` (0x20), **not** NOTIFY (0x10). Renac's own constant is named `BLE_INDICATE_CHARACTERISTIC_UUID`. |
| Config characteristics | `0000fec7-…` / `0000fec8-…` — unused by the Modbus path, but *not* dead: they are Hi-Flying's provisioning channel, driven by the bundled SDK in `com/hiflying/blelink/v1/BleLinker.java` to push WiFi credentials over BLE. Same `fee7` service, different characteristics. |
| `sea_module = AA 55 01 00 00 FE 15 00 00 02 13` | declared in six classes, **dead code**, never sent *by the app*. It is the cloud's module-handshake frame — see section 7 |

---

## 2. Framing

`RenacStorage.TransReadMsg()` / `TransMsg()`, de-obfuscated:

```
read  (fn 3):   [unit] [03] [addr_hi] [addr_lo] [cnt_hi] [cnt_lo] [crc_lo] [crc_hi]
write (fn 6):   [unit] [06] [addr_hi] [addr_lo] [val_hi] [val_lo] [crc_lo] [crc_hi]
write (fn 16):  [unit] [10] [addr_hi] [addr_lo] [cnt_hi] [cnt_lo] [nbytes] [data…] [crc_lo] [crc_hi]
```

* `unit` = `comaddr`, hard-coded `1` in **every** device class. The app never
  scans for a slave id.
* CRC is textbook CRC-16/MODBUS: init `0xFFFF`, poly `0xA001` (the decompiler
  prints it as `40961`), appended **low byte first**.
* Addresses in the frames are the plain register numbers — 10000 is `0x2710`.
  No `-1` offset, no 4xxxx convention.

### How the app *reads* a reply

This is the part worth copying. `Bluttoothlocal`/`BlueComm.onCharacteristicChanged`:

```java
// skip every leading 0xFF and 0x00 byte
int i = 0;
while (i < value.length && (value[i] == (byte)0xFF || value[i] == 0x00)) i++;
if (i == value.length)      return;   // all padding
if (value[i]     != 0x01)   return;   // not our slave
if (value[i + 1] == (byte)0x83) return; // Modbus exception -> discard silently
revbs = Arrays.copyOfRange(value, i, value.length);
```

Two things fall out of that:

1. **The line emits leading `FF`/`00` padding.** The app is built to expect it.
   A strict RTU parser that CRCs the whole buffer, or that assumes byte 0 is the
   unit id, will reject a perfectly good reply.
2. **The app never validates the CRC of a reply.** `checkcrc16()` exists and is
   never called. `TransInfo()` dispatches purely on `byte[1] == 3` and the byte
   count in `byte[2]`. The firmware is evidently sloppy enough that Renac gave up
   on checking.

Timing, from `BlueComm.WriteAndReadData()`: poll for the reply up to **60 × 100 ms
= 6 s**, then **500 ms** between polling cycles. Your 3 s timeout is on the short
side.

---

## 3. The exact frames the app sends

Verified byte-for-byte, CRC recomputed and matched:

| Class field | Bytes on the wire | fn | addr | regs | reply len |
|---|---|---|---|---|---|
| `RenacStorage.re_id1` | `01 03 27 10 00 70 4F 5F` | 3 | 10000 | 112 | 224 B |
| `RenacStorage.re_id2` | `01 03 27 80 00 46 CF 64` | 3 | 10112 | 70 | 140 B |
| `RenacStorage.re_data1` | `01 03 2A F8 00 4C CD D6` | 3 | 11000 | 76 | 152 B |
| `RenacStorage.re_data3` | `01 03 2A F8 00 72 4C 06` | 3 | 11000 | 114 | 228 B |
| `RenacStorage.re_data2` | `01 03 36 B0 00 1B 0B AE` | 3 | 14000 | 27 | 54 B |

Other families, for completeness:

| Class | Frame | addr/regs |
|---|---|---|
| `Renac10.re_1` | `01 03 27 10 00 3E CF 6B` | 10000 / 62 |
| `Renac10.re_2` | `01 65 04 29 04 00 1B  29 CC 00 10  2A 30 00 11  2E E0 00 06 FF D7` | **vendor fn 0x65**: 4 blocks — (10500,27) (10700,16) (10800,17) (12000,6) |
| `Renac50.re_1` | `01 03 27 10 00 26 CF 61` | 10000 / 38 |
| `Renac50.re_data1` | `01 03 4E 20 00 46 D2 DA` | 20000 / 70 |
| `RenacCharger.re_id1` | `01 03 27 10 00 69 8E 95` | 10000 / 105 |
| `Kstar120.sea_sn` | `01 03 0C 80 00 11 87 7E` | 3200 / 17 |

Function `0x65` is a Renac extension — "read N scattered blocks in one frame".
Only the older `Renac10` (devstyle 1) uses it; the storage family does not.

### Session sequence (`BlueComm.RenacStorageRunnable`)

```
connect
  -> re_id1          (10000, 112)   model name + serial
  -> re_id2          (10112,  70)   firmware versions
  loop every 500 ms:
     -> drain the settings queue (fn 3 / 6 / 16 from the UI)
     -> re_data3     (11000, 114)   three-phase   [stostyle 1]
        or re_data1  (11000,  76)   single-phase  [stostyle 0]
     -> re_data2     (14000,  27)   energy counters
```

If `re_id1` does not answer, the app never enters the loop at all.

---

## 4. Which class is your N3-HV

`RenacStorage` with `stostyle = 1` (`devstyle = 3`). This is not a guess — the
register numbers in your *N3 HV Modbus Protocol V1.00* table land exactly on the
fields `RenacStorage` decodes:

| Your doc says | `RenacStorage` decodes |
|---|---|
| Model, 10000, 16 regs | `TransId1`: `modulename` = bytes 3…34 of the 10000 block = regs 10000–10015 |
| Inverter SN, 10016, 16 regs | `TransId1`: `sn` = bytes 35…66 = regs 10016–10031 |
| Inverter Master Ver, 10176 | `TransId2`: offset 131 in the 10112 block = reg 10176, `/100` |
| Protocol Ver, 10181 | `TransId2`: offset 141 = reg 10181, `/100` |
| Battery voltage, 11020 | `TransRunValue3`: offset 43 in the 11000 block = reg 11020, signed `/10` |
| BMS-SOC, 11026 | offset 55 = reg 11026, unsigned |
| Inverter state, 11057 | offset 117 = reg 11057, enum |

Every single one matches. The document and the APK describe the same device.

---

## 5. Register map (RenacStorage / N3-HV)

`stostyle 1` = three-phase, `stostyle 0` = single-phase. Signed means the app
casts to `short`; scale is a divisor.

### Block `10000`, 112 regs — identity

The app decodes only the first two fields. The rest of what it pulls down, per
the V1.00 document:

| Regs | Field | Type | App decodes? |
|---|---|---|---|
| 10000–10015 | Model | 32-byte ASCII, `0x00` rendered as space, trimmed | ✓ |
| 10016–10031 | Inverter SN | 32-byte ASCII, same treatment | ✓ |
| 10032–10047 | BMS Master SN | 32-byte ASCII | |
| 10048–10063 | BMS Slave1 SN | 32-byte ASCII | |
| 10064–10079 | BMS Slave2 SN | 32-byte ASCII | |
| 10080–10095 | BMS Slave3 SN | 32-byte ASCII | |
| 10096–10111 | BMS Slave4 SN | 32-byte ASCII | |

### Block `10112`, 70 regs — firmware

This is why the second read starts at the odd-looking 10112: it is BMS Slave5 SN
through Protocol Ver, and the app only wants the tail.

| Regs | Field | Type | App decodes? |
|---|---|---|---|
| 10112–10127 | BMS Slave5 SN | 32-byte ASCII | |
| 10128–10143 | BMS Slave6 SN | 32-byte ASCII | |
| 10144–10159 | BMS Slave7 SN | 32-byte ASCII | |
| 10160–10175 | BMS Slave8 SN | 32-byte ASCII | |
| 10176 | Inverter Master Ver | U16 /100 | ✓ |
| 10177 | Inverter Slave Ver | U16 /100 | ✓ |
| 10178 | Inverter HMI Ver | U16 /100 | ✓ |
| 10179 | BMS Master Ver | U16 /100 | ✓ |
| 10180 | BMS Slave Ver | U16 /100 | ✓ |
| 10181 | Protocol Ver | U16 /100 | ✓ |

Registers 10182 (HMI Boot Ver) and 10200–10215 (self-test results) exist in the
document but fall outside both blocks the app reads.

### Block `11000`, 114 regs (three-phase) — live values

| Reg | Field | S | Scale | Unit |
|---|---|---|---|---|
| 11000 | pv1_voltage | ✓ | 10 | V |
| 11001 | pv1_current | ✓ | 10 | A |
| 11002 | pv1_power | ✓ | 1 | W |
| 11003 | pv2_voltage | ✓ | 10 | V |
| 11004 | pv2_current | ✓ | 10 | A |
| 11005 | pv2_power | ✓ | 1 | W |
| 11016 | internal_temp | ✓ | 10 | °C |
| 11017 | charger_temp | ✓ | 10 | °C |
| 11018 | boost_temp | ✓ | 10 | °C |
| 11019 | inverter_temp | ✓ | 10 | °C |
| 11020 | battery_voltage | ✓ | 10 | V |
| 11021 | battery_current | ✓ | 10 | A |
| 11022 | battery_power | ✓ | 1 | W |
| 11023 | bms_voltage | ✓ | 10 | V |
| 11024 | bms_current | ✓ | 10 | A |
| 11025 | bms_temp | ✓ | 10 | °C |
| 11026 | soc | | 1 | % |
| 11027 | soh | | 1 | % |
| 11040 | battery_status | | | enum, see below |
| 11041 | cycle_count | | 1 | |
| 11056 | battery_capacity | | 1 | Ah |
| 11057 | inverter_state | | | enum |
| 11058 | bms_state | | | 0 = NG, else OK |
| 11059 | meter_state | | | 0 = NG, else OK |
| 11060 | fault word 1 | | | bitmask `stofault1` |
| 11061 | fault word 2 | | | bitmask `stofault2` |
| 11066 | fault word 7 | | | bitmask `stofault7` |
| 11067 | fault word 8 | | | bitmask `stofault8` (BMS fault) |
| 11068 | fault word 9 | | | bitmask `stofault9` (BMS protect) |
| 11069 | fault word 10 | | | bitmask `stofault10` (BMS alarm) |
| 11076 / 11077 / 11078 | grid_voltage R / S / T | | 10 | V |
| 11079 / 11080 / 11081 | inverter_current R / S / T | ✓ | 10 | A |
| 11082 / 11083 / 11084 | inverter_power R / S / T | ✓ | 1 | W |
| 11085 / 11086 / 11087 | grid_freq R / S / T | | 100 | Hz |
| 11088 / 11089 / 11090 | eps_voltage R / S / T | ⚠ | 10 | V |
| 11091 / 11092 / 11093 | eps_current R / S / T | ✓ | 10 | A |
| 11094 / 11095 / 11096 | eps_power R / S / T | ⚠ ✓ | 1 | W |
| 11097 | eps_freq | | 100 | Hz |
| 11098 / 11099 / 11100 | meter1_power R / S / T | ✓ | 1 | W |
| 11101 | **meter1_power total** (grid import +, export −) | ✓ | 1 | W |
| 11102–11105 | meter2 R / S / T / total | ✓ | 1 | W |
| 11106–11109 | meter3 R / S / T / total | ✓ | 1 | W |
| 11110 / 11111 / 11112 | load_power R / S / T | ✓ | 1 | W |
| 11113 | **load_power total** | ✓ | 1 | W |

⚠ marks the two places where the app and the document disagree about
signedness. The table above follows the **document**: 11088–11090 are U16 (the
app reads them signed) and 11094–11096 are I16 (the app reads them unsigned).
Section 8 has the consequences.

The document also defines these inside the same 11000 block. The app reads them
down the wire and then throws them away, so they are free:

| Reg | Field | S | Scale | Unit |
|---|---|---|---|---|
| 11028 | BMS charge cutoff voltage | ✓ | 10 | V |
| 11029 | BMS discharge cutoff voltage | ✓ | 10 | V |
| 11030 | BMS max charge current | ✓ | 10 | A |
| 11031 | BMS max discharge current | ✓ | 10 | A |
| 11032 / 11033 | BMS max / min cell voltage | ✓ | 1000 | V |
| 11034 / 11035 | BMS max / min cell voltage index | | 1 | |
| 11036 / 11037 | BMS max / min cell temp | ✓ | 10 | °C |
| 11038 / 11039 | BMS max / min cell temp index | | 1 | |
| 11042 / 11043 | BMS max / min module voltage | ✓ | 10 | V |
| 11044 / 11045 | BMS max / min module voltage index | | 1 | |
| 11046 / 11047 | BMS max / min module temp | ✓ | 10 | °C |
| 11048 / 11049 | BMS max / min module temp index | | 1 | |
| 11050 / 11051 | forbid charge / discharge flag | | 1 | |
| 11052 | total cell count | | 1 | |
| 11053 | module count | | 1 | |
| 11054 | cells per module | | 1 | |
| 11055 | voltage level | | 1 | V |

Fault words 11062–11065 (Fault 3–6) and 11070–11075 (Fault 11–16) are also in
the block. Neither the document nor the app names their bits.

### Block `11000`, 76 regs (single-phase) — the differences

**This variant does not apply to your N3-HV.** The V1.00 document marks
11006–11015 as *Reserved*, which is independent confirmation that the N3-HV is
the `stostyle 1` / three-phase product and reads `re_data3`. The map below is
`RenacStorage.TransRunValue1`, i.e. Renac's single-phase storage line, and it is
here only so the APK analysis is complete.

11000–11005 and 11016 onward are identical. In place of the three-phase tail:

| Reg | Field | S | Scale |
|---|---|---|---|
| 11006 | grid_voltage | | 10 |
| 11007 | inverter_current | ✓ | 10 |
| 11008 | inverter_power | ✓ | 1 |
| 11009 | grid_freq | | 100 |
| 11010 | eps_voltage | ✓ | 10 |
| 11011 | eps_current | ✓ | 10 |
| 11012 | eps_power | | 1 |
| 11013 | eps_freq | | 100 |
| 11014 | meter_power | ✓ | 1 |
| 11015 | load_power | ✓ | 1 |

### Block `14000`, 27 regs — energy counters

All `/10`, all kWh. `u32` is big-endian, high word first.

| Regs | Field | Width |
|---|---|---|
| 14000–14001 | pv_total | u32 |
| 14002 | pv_today | u16 |
| 14003–14004 | charge_total | u32 |
| 14005 | charge_today | u16 |
| 14006–14007 | discharge_total | u32 |
| 14008 | discharge_today | u16 |
| 14009–14010 | feedin_total | u32 |
| 14011 | feedin_today | u16 |
| 14012–14013 | consume_total | u32 |
| 14014 | consume_today | u16 |
| 14015–14016 | output_total | u32 |
| 14017 | output_today | u16 |
| 14018–14019 | input_total | u32 |
| 14020 | input_today | u16 |
| 14021–14022 | load_total | u32 |
| 14023 | load_today | u16 |
| 14024–14025 | eps_total | u32 |
| 14026 | eps_today | u16 |

### Enums (`PubCal`)

`inverter_state` (11057): 0 Wait, 1 Check, 2 Normal, 3 StandBy, 4 Fault,
5 Permanent, 6 Update, 7 Eps check, 8 Eps, 9 Self test, 10 Idle.

`battery_status` (11040) is a bitfield. Per the document:

| Bits | Meaning |
|---|---|
| 2–0 | 0 Sleep, 1 Charge, 2 Discharge, 3 Idle, 4–7 reserved |
| 3 | forced charge request |
| 4 | balance charge request |
| 15–5 | reserved |

The app gets bits 2–0 right and nothing else: `GetRenacStorageBatteryState`
computes `i2 = i % 8` and then tests `(i2 >> 3) & 1` for the balance-charge
flag, which is always 0 after the mask. So the app can never report either
request bit, and the flag it *meant* to read is bit 4, not bit 3.

Fault words 11060–11075 are "Fault 1" … "Fault 16" in the document, all
`Bitfield16`, **with no bit names given for any of them**. The APK is the only
source for the names, and it covers six of the sixteen:

| Reg | Document | APK array |
|---|---|---|
| 11060 | Fault 1 | `stofault1` |
| 11061 | Fault 2 | `stofault2` |
| 11066 | Fault 7 | `stofault7` |
| 11067 | Fault 8 | `stofault8` — BMS fault |
| 11068 | Fault 9 | `stofault9` — BMS protect |
| 11069 | Fault 10 | `stofault10` — BMS alarm |

The numbering lines up exactly, which is a good independent check that the APK's
arrays belong to these registers.

Fault bit names are the `PubCal.stofault*` arrays. The app renders the word as a
16-character binary string and indexes `stofault[15 - charPos]`; since `charPos`
0 is the MSB, that reduces to **bit *n* → `stofault[n]`** — the arrays are in
plain bit order. So `stofault1` is bit 0 `HardwareTrip`, bit 1 `MainsLost`,
bit 2 `GridVoltFault`, and so on.

(The `stofault8` branch has an off-by-one in the shipped app — it uses
`[16 - i8]` when the fault string is already non-empty, which indexes past the
end of a 16-element array. Mentioned only so it is not mistaken for a different
bit layout.)

### Settings blocks (fn 3 read / fn 6 / fn 16 write)

`RenacStorage.ReadValue()` reads these on demand. Read-only probing is safe;
writing is not.

Names below are from the document, not guessed:

| App reads | Regs (3ph) | Registers covered |
|---|---|---|
| 20000 | 1 | System on/off (0 off, 1 on) |
| 20001 | 6 | RTC year (0–99, +2000) / month / day / hour / minute / second → 20001–20006 |
| 20007 | 6 → 7 | Language, Grid code, Meter1/2/3 Addr (1–247), **Modbus Addr (20012, 1–247)**, Password (20013) |
| 20100 | 6 / 9 | DRM0, Meter1 en, Remote power control en, ISO, GFCI, Anti-islanding, Freq protect restrictive, Ground connect check, Phase unbalance → 20100–20108 (20109 Bypass Relay is defined but not read) |
| 21000 | 1 | Work mode: 0 Self use, 1 ForceTimeUse, 2 Back up, 3 Feed-in First |
| 21001 | 10 | P1/P2 grid-charge flags and charge window start/end hour+minute → 21001–21010 |
| 21011 | 5 | Backup grid-charge flag + window → 21011–21015 |
| 21016 | 5 | Max charge current (/10 A), max discharge current (/10 A), min SoC (5–100 %), min SoC on-grid, export limit (W) → 21016–21020 |
| 21022 | 1 | — (21021 is Power limit percent, I16, −100…100) |
| 22000 | 9 | Con/Recon enable bitfield + connect/reconnect times, gradients, voltage and frequency limits → 22000–22008 |
| 22100 | 11 | Voltage protect enable bitfield + high/low limits 1 & 2 with trip times, 10-min high limit, activate bitfield → 22100–22110 |
| 22200 | 10 | Frequency protect enable + high/low limits 1 & 2 with trip times, activate bitfield → 22200–22209 |
| 23000 | 11 | P(f): enable, mode, enter delay, over/under-frequency start, droop, recover, power recover gradient and delay → 23000–23010 |
| 23100 | 9 | P(u): enable, V1–V4, P1, P4, delay, decline gradient → 23100–23108 |
| 23200 | 24 | Reactive power: control mode (0 None, 1 Fixed PF, 2 Fixed Q, 3 CosphiP, 4 Q(U)), Fixed PF/Q, CosphiP curve, Q(U) curve → 23200–23223 |
| 23300 | 16 | UVRT/OVRT enable, V1–V3 and T1–T3 for each, zero-current under/over voltage thresholds → 23300–23315 |
| 23400 | 2 | ROCOF enable, ROCOF threshold (/10 Hz/s) → 23400–23401 |

Write-only commands at 24000–24004: Clear events, Clear energy, Factory reset,
Battery awake, Start Self-Test.

Note 20012 — the Modbus slave address is settable, range 1–247, and the document
**does not state the factory default**. The app hard-codes 1 and never scans.

---

## 6. Why TCP 8899 is silent — settled

**Socket A is not connected to the UART, in either direction. It is a vestigial
factory default. No configuration on this firmware can revive it.**

> This section is about the **inbound** listener on 8899, and it still holds.
> It does not mean the module has no TCP path to the UART: it opens one itself,
> outbound, to Renac's server, and tunnels raw Modbus RTU over it. See
> section 7 — that connection is the practical way in.

This section used to hold seven ranked theories. All of them have been measured
and all of them are wrong. What follows is the evidence, kept because the
negative results are the valuable part — they stop anyone repeating the work.

### The decisive measurement

The measurement held BLE and TCP open at the same time and sent a request
down one channel while watching both. BLE, socket A and socket B all bridge to
the same UART, so the inverter's reply has to surface *somewhere*.

```
                             on BLE     on TCP
  sent over TCP                   0          0
  sent over BLE                 229          0
```

Read both rows, because each kills a different half:

- **Row 1** — a request written to socket A produced nothing anywhere, while the
  identical bytes over BLE produced a full 229-byte reply seconds later. So
  network→UART is dead: bytes written to 8899 never reach the inverter.
- **Row 2** — a reply that definitely crossed the UART did not appear on the TCP
  socket. So UART→network is dead too.

Socket A accepts TCP connections and silently discards everything.

### Nothing in Renac's own software uses it

The decompiled app settles motive. `8899` does not appear anywhere in the Java
sources, and the entire `app.renac.ess` package contains no sockets at all — no
`new Socket(`, no `ServerSocket`, no `DatagramSocket`, no `InetSocketAddress`.
The only networking outside BLE is Hi-Flying's provisioning SDK
(`com/hiflying/blelink/v1/BleLinker.java:59`, UDP 48899) pushing WiFi
credentials.

Port 8899 listens because stock HF firmware always listens. It is the factory
default value of `AT+NETP` that the vendor never rewired to anything. Nothing is
expecting a client there.

### Everything that was ruled out, and how

| Theory | Ruled out by |
|---|---|
| Wrong framing | BLE sends byte-identical frames and works |
| Block-boundary / sub-range reads | V1.00 §2 permits single-register reads; and TCP is silent for whole blocks too |
| Solarman V5 wrapper | no numeric serial; the module's 16-character label serial contains a letter |
| Module in command mode | `AT+TMODE` = `throughput` |
| Module dialling out instead of listening | `AT+NETP` = `TCP,Server,8899` |
| 4th `NETP` field scopes the socket to an address | measured, see below: the module held `10.10.100.254` itself and socket A still discarded |
| Modbus TCP↔RTU gateway expecting MBAP | all `AT+MODBUS*` / `AT+PROTOCOL*` unsupported |
| Channel tagging mangling the stream | `AT+NETPIDEN` / `AT+NETPID` unsupported |
| Single-client limit, socket already held | `AT+MAXSK` = 5, and `AT+TCPLK` = `off` before connecting |
| UART contention with BLE | BLE polls happily while `TCPLKB` = on |
| BLE session holding socket A | connecting over BLE leaves `AT+TCPLK` = `off` |
| Wrong slave id | BLE proves unit 1 answers |
| Reply fragmentation / `FF` padding | cannot cause *total* silence; handled anyway |

`AT+TCPLK` does flip `off`→`on` when you connect, so the connection genuinely
lands on socket A. It simply goes nowhere from there.

### The `NETP` address field, measured

`AT+NETP` reports `TCP,Server,8899,10.10.100.254`, and that address is also
`AT+LANN` — the module's AP-side address. The obvious theory is that the fourth
field scopes socket A: the listener accepts on any interface, then something
above the stack compares the connection's local address against it and drops
whatever does not match. It fits the symptom exactly — handshake completes,
`AT+TCPLK` flips `on`, payload vanishes in both directions.

It is wrong. On 2026-09-20 the module was given `10.10.100.254` on its **STA**
interface: a DHCP static lease against the module's MAC plus a matching subnet
on the router, so the address was genuinely the module's own and no NAT sat in
the path. A TCP client then connected to `10.10.100.254:8899`:

```
TCP port is open.
  modbus-tcp     Model  FAIL  no data returned
  rtu-over-tcp   Model  FAIL  no data returned
```

Repeated from `10.10.100.250`, on the same L2 segment with no router in the
path, so the module's local address matched the configured one *and* nothing
could be lost in transit — the one combination never previously tested:

```
Probing 10.10.100.254:8899
TCP port is open.
  modbus-tcp     Model  FAIL  no data returned
  rtu-over-tcp   Model  FAIL  no data returned
NOTHING READ.
```

Same silence. The field does not scope the socket, and with routing eliminated
there is nothing left to blame. **Socket A is settled: it accepts TCP and
discards payload in both directions under every configuration reachable without
reflashing.**

An earlier version of the table above ruled this out on the grounds that
`AT+LANN` = `10.10.100.254`, so the value "is just the AP address". That was
circular — the two matching is what the theory *predicts*, not evidence against
it. The measurement is the real refutation.

### Side effect: UDP discovery is subnet-local

Moving the STA into `10.10.100.0/24` broke UDP discovery from a laptop on
the LAN's own subnet, while TCP to the same address still completed a handshake. Giving
the laptop a static `10.10.100.250` restored it immediately, and the module
confirmed the new address itself:

```
HF-A11ASSISTHREAD
    10.10.100.254,<the module's MAC>,SOLAR_WIFI
AT+NETP
    +ok=TCP,Server,8899,10.10.100.254
```

So the module really does hold `10.10.100.254` on its STA interface — its own
discovery reply says so — which is what makes the socket A measurement above
valid.

An earlier draft of this section read the TCP-works/UDP-fails split as an address
collision with the AP interface, and concluded from it that the AP was up. That
was wrong. `10.10.100.250` sits in the same `/24` the AP would be claiming, so a
collision would have swallowed that reply too. It did not. **`AT+WMODE` still has
never returned a usable answer, and whether the AP is running remains unknown.**

Why the off-subnet reply never arrived is not isolated. Candidates are a MikroTik
`forward` rule that does not cover the newly added subnet, and the module's own
routing on the STA netif. It has no bearing on socket A and was not pursued.

`AT+Q` returning nothing is normal — it ends the UDP AT session rather than
answering it.

### Port 80 is not a way in either

The dongle serves a hand-written page on port 80 asking you to change a username
and password:

```
<html><body><form><h4>Modify the user name and password: </h4>
UserName:<input type="text" name="User name" value=""/>
PassWord:<input type="text" name="Password" value=""/>
<input type="submit" value="SUBMIT" disabled="disabled" id="SUBMIT"/>
<input type="checkbox" name="policy" value="policy" id="policy"/>
I have read and agreed Privacy Policy ...
```

Three things about that response matter.

1. **It arrives with no HTTP headers at all** — `curl` needs `--http0.9` to
   accept it. A stock Hi-Flying httpd sends a proper status line, so this page is
   written by the dongle vendor, not the HF stock web UI.
2. **The `<form>` has no `action=` and no `method=`**, so SUBMIT sends
   `GET /?User+name=...&Password=...&policy=policy`. The write is a GET with a
   query string, nothing more.
3. **Both `value=` are empty and there is no "old password" field.** Whatever you
   type simply overwrites what is stored.

### What port 80 showed

The question that decides whether port 80 is worth anything: **does the server
return the same page no matter what you ask for?** Hand-rolled embedded servers
usually ignore the request line entirely and write one canned page to every
connection.

It does. A nonsense control path (`/zzzz-no-such-page-renacprobe`) comes back
byte-identical to `/` — the same ~780 bytes — as does every page name used by
the HF stock web UI (`iweb.html`, `mode.html`, `other.html`, `adv.html`, …) and
by Solarman-style loggers on the same module family (`config_hide.html`,
`protect.html`, …). There is no 404, no static file serving, no hidden config
page. The request line is not parsed.

So port 80 is one hard-coded response. It exposes no settings, reaches no UART,
and ends the search.

---

## 7. The cloud connection — raw Modbus RTU over outbound TCP

**The module does bridge its UART to TCP. It just dials out instead of
listening.** Section 6 stands as written — inbound socket A on 8899 is dead —
but it is only half the picture, and the other half is the way in.

Sources: `conversation.yml`, a 106-packet capture of the module
talking to Renac's server, 2026-09-18 22:42:34 → 22:44:52 UTC, and
`conversation2.raw`, a second session ~38 minutes later (the captures are
not in this repository: they hold the unit's serials and addresses).

A third session, `conversation3.raw`, was captured at
10:53 local with the inverter **running** rather than idle — the only capture
showing it generating.

**All three agree, which is what makes replay viable:**

| | |
|---|---|
| Init requests (cloud → device) | **all four byte-for-byte identical in all three captures** |
| Init replies (device → cloud) | identical except **one byte**, below |
| Poll sequence | **first 47 operations identical**, same order |
| Difference | capture 1 runs two extra reads at the end (`11011 × 1`, `10618 × 2`) and one extra `AA 55` keepalive |
| Varying payloads | the RTC write, and the live registers |

So there is **no nonce, no session key and no challenge**. The frames the server
sends are a fixed byte string, and replaying them is sufficient.

The one varying byte is in the *reply* to `cmd 0x15 / sub 0x0B`, at offset 3 of
its 8-byte payload:

```
c1, c2:  aa55010000fe158b08 01 32 32 25 30 30 32 30 03f2
c3:      aa55010000fe158b08 01 32 32 28 30 30 32 30 03f5
                                  ^^ 0x25 (37) -> 0x28 (40), checksum follows
```

37 and 40 look like a live metric — signal strength is the obvious guess, but
three samples is not enough to claim it. Nothing the emulator sends depends on
it, and the checksum is recomputed from the received bytes, so the variation is
handled transparently.

**Guess (2026-10-15), not proven: the rest of the payload is the module
serial.** Bytes 1–7 read as ASCII are `2 2 % 0 0 2 0`; the module serial in
the `0x15 / 0x00` reply (table in 7.1) begins `2200020`, the same seven
characters except the third, which is the varying byte. So the payload looks
like `01`, then the serial's first seven characters with the third replaced by
the metric. One module only; a module with another serial would settle it. The
fake module (`tests/mocks/mock_hf_lpt270.py`) builds its reply this way.

| Peer | Address | Role |
|---|---|---|
| 0 | the module's LAN IP, port `53671` | the HF-LPT270. Ephemeral source port, so **it opened the connection** |
| 1 | `47.245.183.205:20145` | Renac's server, and **the Modbus master** |

The inverter is the slave in its own cloud session. Every register read below is
the *server* asking and the inverter answering.

### 7.1 Two protocols on one socket

The session opens with a short module-management handshake in the `AA 55`
framing, then switches to bare Modbus RTU and never goes back.

```
AA 55 | 01 00 00 FE | cmd | sub | len | payload…(len bytes) | chk_hi chk_lo
```

* `01 00 00 FE` is fixed in all nine frames.
* `sub | 0x80` marks a reply.
* `len` is one byte and counts payload only.
* **Checksum is a 16-bit big-endian sum of every byte from `AA` up to the
  checksum itself.** Verified against all nine frames, zero mismatches.

This is the same 11-byte constant the app carries as `sea_module`
(`AA 55 01 00 00 FE 15 00 00 02 13`, section 1) — with `cmd 0x15`, `sub 0x00`,
no payload. The app declares it in six classes and never sends it. **The cloud
sends it.** That is where the constant comes from, and section 1 was right to
call it dead code *on the app side*.

| Frames | cmd | sub | Reply | Content |
|---|---|---|---|---|
| 36 / 37 | `0x15` | `0x00` | 161 B | the module's serial, MAC, firmware `LFWIFI202406060000` and LAN IP |
| 41 / 42, 44 / 45 | `0x15` | `0x0B` | 19 B | 8-byte payload `01 32 32 25 30 30 32 30`, sent twice, purpose unknown |
| 46 / 47 | `0x0E` | `0x00` | 45 B | the inverter's serial, 16 digits |
| 176 | `0x15` | `0x02` | — | keepalive, 100 s after the poll finished |

Then, with no transition marker of any kind, `01 03 27 10 00 20 4F 63` goes
straight down the same socket. No MBAP header, no envelope, CRC-16 intact —
byte-for-byte what the UART expects, exactly as over BLE. **96 of 97 Modbus
frames in the capture carry a valid CRC.**

### 7.2 The poll sequence

49 operations. The server does not use the app's five fixed blocks; it walks a
wider list, including registers absent from the vendor document, and it reads
identity as one `10000 × 32` instead of the app's `10000 × 112`.

Notable: the server probes **10500 × 64** and **10600 × 18** and takes
`exception 02` on both. Those are `Renac10.re_2`'s blocks (section 3). It is
running the same device-family detection the app does, and being told no.

### 7.3 The cloud writes the clock

One write in the whole session, at t+15.7 s:

```
01 10 4E 21 00 06 0C 00 1A 00 09 00 13 00 01 00 2A 00 31 <crc>
   fn 0x10  20001  x6  12B  26    9     19    1     42    49
```

`fn 0x10` into 20001–20006 — **RTC set to 2026-09-19 01:42:49**. It reads the
block back at t+17.0 s and gets its own values.

This explains the dead `AT+NTPTM` in section 6: the clock is not NTP-synced, it
is pushed by the cloud over Modbus. Cut the cloud off and nothing sets the time.

### 7.4 Renac's server has a CRC bug, and it is reproducible

Packet 166, server → inverter:

```
sent    01 03 2B 71 00 03 9D F4
correct 01 03 2B 71 00 03 5C 34
```

The inverter correctly ignored it and stayed silent. The server then burned a
10-second timeout before moving on.

**The identical bad frame appears in capture 2** (line 101, same bytes, same
wrong CRC). Two sessions 38 minutes apart, same defect: the server's `11121 × 3`
request is permanently mis-CRC'd. It is not line noise — it is a bug in Renac's
server, and it means register 11121 is never actually read in production.

Capture 2 holds one other bad frame, `A0 20 20 01 79 84` at line 28, where the
reply to `20000 × 1` should be `01 03 02 00 01 79 84`. The trailing four bytes
survive; the first three are mangled and one byte is missing. That one looks
like genuine corruption on the link rather than a protocol feature.

### 7.5 What the device reported

Values from this capture, for reference when comparing against your own reads:

| | |
|---|---|
| Model / SN | `N3-HV-5.0` / not published |
| Versions (10176–10181) | master **1.21**, slave 1.00, HMI 2.03, BMS 0.00 / 0.00, **protocol 0.39** |
| Inverter state (11057) | 0 = **Wait** |
| Faults (11060–11075) | all sixteen words zero |
| Battery | **not present** — BMS connect NG, 0.6 V, SOC 0, cell count 0 |
| Grid | 236.7 / 235.5 / 237.1 V, 50.01 Hz |
| PV | 3.4 V / 2.7 V — dark, consistent with the capture time |
| Load / Meter1 | 216 W / 217 W, Meter1 connected |
| Energy | PV 118.4 kWh, load 208.3, consumption 100.8, feed-in 3.6 |

Note the device reports **Protocol Ver 0.39** while the document is titled V1.00.

### 7.6 Registers the document does not have

All confirmed answering in this capture, with valid CRCs:

| Registers | Result |
|---|---|
| 50028 / 50029 / 50030 | `0` / `12` / `0` — a live 3-register window |
| 50031 / 50032 / 50035 | exception 02 |
| 20114–20131 | all answer, though the document jumps 20109 → 21000. 20130 = 4200 |
| 11124 × 34 | answers past the documented end of the block (11113). **Solved — see below** |
| 23620 × 7 | answers, but see below |
| 23640 / 23660 | exception 02 |

Later findings about these registers, the other ones the document lacks, the
values of this unit and Renac's web page are kept in
[N3-HV-MODBUS-PROTOCOL-V1.00.md](N3-HV-MODBUS-PROTOCOL-V1.00.md), part B.

**11124–11157 are I32 mirrors of the power registers.** Capture 3 settled this:
with the inverter generating, the values stop being ambiguous zeros and line up
exactly with the I16 block, big-endian signed 32-bit, two registers per field:

| I32 | Field | c3 | I16 source | c3 |
|---|---|---|---|---|
| 11124 | battery power | −3 | 11022 | −3 |
| 11126 / 11128 / 11130 / 11132 | Meter1 R / S / T / total | 437 / −189 / −243 / 4 | 11098–11101 | 436 / −191 / −243 / 1 |
| 11134–11140 | Meter2 R / S / T / total | 0 | 11102–11105 | 0 |
| 11142–11148 | Meter3 R / S / T / total | 0 | 11106–11109 | 0 |
| 11150 / 11152 / 11154 / 11156 | Load R / S / T / total | 741 / 38 / 41 / 820 | 11110–11113 | 740 / 37 / 42 / 819 |

Small deltas are sampling skew between the two reads in the same session.

**Prefer these over the I16 registers.** They carry unambiguous sign and cannot
clip at ±32767 W, which also sidesteps the signedness disagreement in section 8
entirely. The vendor document does not mention them.

**23620 is not data.** Its reply is CRC-valid but reads
`44 00 07 56 4D 00 00 …` — bytes 3–7 of its own request
(`01 03 5C 44 00 07 56 4D`). The window appears to overlap a comms buffer rather
than hold registers. Do not read meaning into the values. Hypothesis, not
measurement.

### 7.7 The consequence: local control without touching 8899

The module already speaks unwrapped Modbus RTU over TCP to whatever answers at
`47.245.183.205:20145`. Redirect that endpoint — DNS override, static route, or
firewall DNAT to a local listener — and the inverter polls *you*, read-only, no
writes and no firmware changes.

**Dialling the module yourself cannot work, and the handshake is not why.** A
client that opens the connection to the module gets no answer on this firmware:

* the handshake is static (above), so replaying it is correct;
* port 8899 accepts TCP and discards everything both ways (section 6);
* in both captures the module was the TCP *client*. Nothing inbound reaches the
  UART.

---

## 8. Cross-check against "RENAC N3 HV Modbus Protocol V1.00"

The official PDF is 17 pages: data types, three function codes, and one long
register table. I extracted all 295 rows of that table by clustering the PDF
text fragments into columns, then compared it field by field against what the
APK does.

### What the document confirms

* **Function codes.** `0x03` read, `0x06` write single, `0x10` write multiple.
  Exactly the three the APK implements, and nothing else. The vendor function
  `0x65` in `Renac10` is not in this document — consistent with it belonging to
  a different product family.
* **Register addresses** of all five blocks the app reads.
* **52 of 58** live-value registers match on both type and gain.
* **All 18 energy fields** at 14000–14026 match exactly, including which are
  U32 and which are U16.
* **All six version registers** at 10176–10181.
* **Model at 10000 ×16, Inverter SN at 10016 ×16**, both `STR`.
* **Inverter state at 11057**: the document's 0–10 enum is identical to
  `PubCal.GetRenacStorageState`, value for value.
* **11058 / 11059**: 0 = NG, 1 = OK.
* **Fault word numbering.** 11060–11075 are Fault 1–16; the APK's
  `stofault1/2/7/8/9/10` land on 11060/11061/11066/11067/11068/11069. The
  numbering agrees, which is a genuine independent check.
* **11006–11015 Reserved**, confirming the N3-HV is the three-phase variant.

### What the document corrects

Three real errors, all of them cases where I had faithfully reproduced the app
and the app is wrong:

| Reg | App does | Document says | Impact |
|---|---|---|---|
| 11094–11096 Eps-R/S/T Power | unsigned | **I16, signed** | **Real.** Negative EPS power reads as ~65000 W instead of a small negative number. Fixed. |
| 11088–11090 Eps-R/S/T Voltage | signed | **U16, unsigned** | Cosmetic — identical below 3276.7 V. Fixed for correctness. |
| 11040 battery status | bits 2–0 only | bit 3 forced charge request, bit 4 balance charge request | The app's balance-charge test is dead code (`(i % 8) >> 3`), and it was aimed at the wrong bit anyway. Fixed. |

### What the document does not say

* **Nothing about the physical layer.** No baud rate, no parity, no stop bits,
  no mention of RS485 or RS232. Section 2 of this file — CRC polynomial, byte
  order, the `FF`/`00` padding, the 6 s/500 ms timing — is APK-derived and has
  no counterpart in the official document.
* **No fault bit names.** All sixteen fault words are listed as bare
  `Bitfield16`. The APK's `stofault*` arrays are the only published source for
  what the bits mean.
* **No default slave address.** 20012 is documented as settable 1–247 with no
  factory value given.
* **Nothing about block boundaries** — and in fact it says function `0x03`
  reads "a single register or multiple registers", which argues against the
  block-boundary theory. See section 6.

### What the APK adds

* The wire format itself — framing, CRC, padding, timing, what a reply looks
  like.
* Fault bit names for 6 of the 16 fault words.
* Which registers actually matter: five blocks out of a 295-row table.
* Proof that slave id 1 works on a real unit.

The two documents are complementary. Neither alone is enough to write a client.

---

## 9. Finding the module over Bluetooth

TCP 8899 is silent, so the BLE path — the one the app actually uses and which
demonstrably works — is the fallback.

One wrinkle worth knowing before you start. The app finds the module over
**classic** Bluetooth discovery and matches the name `HF-LPT270` exactly, then
switches to BLE via `connectGatt()`. A BLE-only scanner sees less. A dual-mode
module normally shares one public address between the two radios, but the BLE
advertisement name can be shortened or absent where the classic name is not.
So a scanner should rank candidates rather than demand an exact name:

| Signal | Score |
|---|---|
| name `HF-LPT270` **and** advertises service `fee7` | 100 |
| name `HF-LPT270` | 90 |
| advertises service `fee7` | 80 |
| name starts `HF-LPT` | 70 |
| name starts `HF-` | 50 |
| name contains `RENAC` or `SOLAR` | 40 |

If nothing scores, look for a strong-RSSI unnamed device near the inverter.
