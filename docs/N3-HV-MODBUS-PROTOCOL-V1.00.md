# RENAC N3 HV Modbus Protocol V1.00

Transcribed from `1708888016361-n3-hv-modbus-protocol-v1.00.pdf` (17 pages,
vendor document, version 1.00; the PDF itself is not in this repo).

This file has two parts:

- **Part A**, sections 1–3 and the transcription notes: the vendor document, a
  faithful dump of its table — names, types, units, gains, addresses,
  quantities and scopes exactly as printed. Nothing found later is written into
  it.
- **Part B**, sections 4–9: what this unit answers, what Renac's web page shows,
  and what we found or guessed about the registers, the ones the document lacks
  included. Every statement there is labelled measured, confirmed or guess
  (section 4).

How the app and the cloud talk to the module — framing, the `AA 55` handshake,
the BLE path, the first capture analysis — is in [PROTOCOL.md](PROTOCOL.md).

Addresses are plain register numbers. The document uses no `4xxxx` convention
and no `-1` offset.

---

## 1. Data types

| Name | Description |
|---|---|
| U16 | Unsigned integer (16 bits) |
| U32 | Unsigned integer (32 bits) |
| I16 | Signed integer (16 bits) |
| I32 | Signed integer (32 bits) |
| STR | Character string |
| Bitfield16 | 16-bit data expressed by bit |
| Bitfield32 | 32-bit data expressed by bit |
| N/A | Not applicable |
| RO | Data that is readable only |
| RW | Data that is readable and writable |
| WO | Data that is writable only |

## 2. Function codes

| Function Code | Meaning | Remarks |
|---|---|---|
| `0x03` | Read registers. | Continuously reads a single register or multiple registers. |
| `0x06` | Write a single register. | Writes into a single register. |
| `0x10` | Write multiple registers. | Continuously writes into multiple registers. |

Only these three are defined. There is no vendor function `0x65` in this document.

## 3. Register definition

### 3.1 Identity — 10000–10175 (STR)

All `RO`, type `STR`, unit `N/A`, gain `1`, quantity `16`.
Scope for every row: **ASCII, left-justified**.

| Address | Signal Name |
|---|---|
| 10000 | Model |
| 10016 | Inverter SN |
| 10032 | Bms Master SN |
| 10048 | Bms Slave1 SN |
| 10064 | Bms Slave2 SN |
| 10080 | Bms Slave3 SN |
| 10096 | Bms Slave4 SN |
| 10112 | Bms Slave5 SN |
| 10128 | Bms Slave6 SN |
| 10144 | Bms Slave7 SN |
| 10160 | Bms Slave8 SN |

### 3.2 Firmware versions — 10176–10182

All `RO`, `U16`, unit `N/A`, gain `1`, quantity `1`.
Scope for every row: **For example, value 100 represents version 1.00**.

| Address | Signal Name |
|---|---|
| 10176 | Inverter Master Ver |
| 10177 | Inverter Slave Ver |
| 10178 | Inverter Hmi Ver |
| 10179 | Bms Master Ver |
| 10180 | Bms Slave Ver |
| 10181 | Protocol Ver |
| 10182 | Hmi Boot Ver |

### 3.3 Self-test results — 10200–10215

All `RO`, `U16`, quantity `1`.

| Address | Signal Name | Unit | Gain |
|---|---|---|---|
| 10200 | Self-Test Result OVP1 | V | 10 |
| 10201 | Self-Test Result OVP1 Time | mS | 1 |
| 10202 | Self-Test Result OVP2 | V | 10 |
| 10203 | Self-Test Result OVP2 Time | mS | 1 |
| 10204 | Self-Test Result UVP1 | V | 10 |
| 10205 | Self-Test Result UVP1 Time | mS | 1 |
| 10206 | Self-Test Result UVP2 | V | 10 |
| 10207 | Self-Test Result UVP2 Time | mS | 1 |
| 10208 | Self-Test Result OFP1 | Hz | 100 |
| 10209 | Self-Test Result OFP1 Time | mS | 1 |
| 10210 | Self-Test Result OFP2 | Hz | 100 |
| 10211 | Self-Test Result OFP2 Time | mS | 1 |
| 10212 | Self-Test Result UFP1 | Hz | 100 |
| 10213 | Self-Test Result UFP1 Time | mS | 1 |
| 10214 | Self-Test Result UFP2 | Hz | 100 |
| 10215 | Self-Test Result UFP2 Time | mS | 1 |

### 3.4 Live values — 11000–11113

All `RO`, quantity `1` unless noted.

#### PV input — 11000–11005

| Address | Signal Name | Type | Unit | Gain |
|---|---|---|---|---|
| 11000 | PV1 voltage | I16 | V | 10 |
| 11001 | PV1 current | I16 | A | 10 |
| 11002 | PV1 power | I16 | W | 1 |
| 11003 | PV2 voltage | I16 | V | 10 |
| 11004 | PV2 current | I16 | A | 10 |
| 11005 | PV2 power | I16 | W | 1 |
| 11006~11015 | **Reserved** (quantity 10; no type, unit or gain given) | | | |

#### Temperatures — 11016–11019

All `I16`, gain `10`, quantity `1`. The Unit cell is blank in the source PDF
(see [Transcription notes](#transcription-notes)).

| Address | Signal Name |
|---|---|
| 11016 | Internal temperature |
| 11017 | Charger temperature |
| 11018 | Boost temperature |
| 11019 | Inverter temperature |

#### Battery and BMS — 11020–11057

| Address | Signal Name | Type | Unit | Gain | Scope |
|---|---|---|---|---|---|
| 11020 | Battery voltage | I16 | V | 10 | |
| 11021 | Battery current | I16 | A | 10 | |
| 11022 | Battery power | I16 | W | 1 | |
| 11023 | BMS-voltage | I16 | V | 10 | |
| 11024 | BMS-current | I16 | A | 10 | |
| 11025 | BMS-temp | I16 | *(blank)* | 10 | |
| 11026 | BMS-SOC | U16 | N/A | 1 | |
| 11027 | BMS-SOH | U16 | N/A | 1 | |
| 11028 | BMS-ChargeCutoffVoltage | I16 | V | 10 | |
| 11029 | BMS-DischargeCutoffVoltage | I16 | V | 10 | |
| 11030 | BMS-MaxChargeCurrent | I16 | A | 10 | |
| 11031 | BMS-MaxDischargeCurrent | I16 | A | 10 | |
| 11032 | BMS-MaxCellVoltage | I16 | V | 1000 | |
| 11033 | BMS-MinCellVoltage | I16 | V | 1000 | |
| 11034 | BMS-MaxCellVoltageNo | U16 | N/A | 1 | |
| 11035 | BMS-MinCellVoltageNo | U16 | N/A | 1 | |
| 11036 | BMS-MaxCellTemp | I16 | *(blank)* | 10 | |
| 11037 | BMS-MinCellTemp | I16 | *(blank)* | 10 | |
| 11038 | BMS-MaxCellTempNo | U16 | N/A | 1 | |
| 11039 | BMS-MinCellTempNo | U16 | N/A | 1 | |
| 11040 | BMS-BasicStatus | Bitfield16 | N/A | 1 | see below |
| 11041 | BMS-Cycletime | U16 | N/A | 1 | |
| 11042 | BMS-MaxModuleVoltage | I16 | V | 10 | |
| 11043 | BMS-MinModuleVoltage | I16 | V | 10 | |
| 11044 | BMS-MaxModuleVoltageNo | U16 | N/A | 1 | |
| 11045 | BMS-MinModuleVoltageNo | U16 | N/A | 1 | |
| 11046 | BMS-MaxModuleTemp | I16 | *(blank)* | 10 | |
| 11047 | BMS-MinModuleTemp | I16 | *(blank)* | 10 | |
| 11048 | BMS-MaxModuleTempNo | U16 | N/A | 1 | |
| 11049 | BMS-MinModuleTempNo | U16 | N/A | 1 | |
| 11050 | Forbid Charge Flag | U16 | N/A | 1 | |
| 11051 | Forbid Discharge Flag | U16 | N/A | 1 | |
| 11052 | Total Cell No | U16 | N/A | 1 | |
| 11053 | Module No | U16 | N/A | 1 | |
| 11054 | Cell No Per Module | U16 | N/A | 1 | |
| 11055 | Voltage level | U16 | V | 1 | |
| 11056 | AH number | U16 | AH | 1 | |
| 11057 | Inverter state | U16 | N/A | 1 | see below |

**11040 BMS-BasicStatus (Bitfield16)**

| Bits | Meaning |
|---|---|
| Bit15–Bit5 | Reserve |
| Bit4 | 0 = Null; 1 = Balance charge request |
| Bit3 | 0 = Null; 1 = Forced charge request |
| Bit2–Bit0 | 0: Sleep, 1: Charge, 2: Discharge, 3: Idle, 4~7: Reserve |

**11057 Inverter state**

| Value | State | Value | State |
|---|---|---|---|
| 0 | Wait | 6 | Update |
| 1 | Check | 7 | EpsCheck |
| 2 | Normal | 8 | Eps |
| 3 | Standby | 9 | SelfTest |
| 4 | Fault | 10 | Idle |
| 5 | Permanent | | |

#### Connection state — 11058–11059

Both `RO`, `U16`, `N/A`, gain `1`, quantity `1`. Scope: **0: NG, 1: OK**.

| Address | Signal Name |
|---|---|
| 11058 | BMS connect state |
| 11059 | Meter1 connect state |

#### Faults — 11060–11075

All `RO`, `Bitfield16`, `N/A`, gain `1`, quantity `1`.
**The document gives no bit definitions for these.**

| Address | Signal | Address | Signal |
|---|---|---|---|
| 11060 | Fault 1 | 11068 | Fault 9 |
| 11061 | Fault 2 | 11069 | Fault 10 |
| 11062 | Fault 3 | 11070 | Fault 11 |
| 11063 | Fault 4 | 11071 | Fault 12 |
| 11064 | Fault 5 | 11072 | Fault 13 |
| 11065 | Fault 6 | 11073 | Fault 14 |
| 11066 | Fault 7 | 11074 | Fault 15 |
| 11067 | Fault 8 | 11075 | Fault 16 |

#### Grid, inverter, EPS, meters, load — 11076–11113

| Address | Signal Name | Type | Unit | Gain |
|---|---|---|---|---|
| 11076 | Grid-R Voltage | U16 | V | 10 |
| 11077 | Grid-S Voltage | U16 | V | 10 |
| 11078 | Grid-T Voltage | U16 | V | 10 |
| 11079 | Inv-R Current | I16 | A | 10 |
| 11080 | Inv-S Current | I16 | A | 10 |
| 11081 | Inv-T Current | I16 | A | 10 |
| 11082 | Inv-R Power | I16 | W | 1 |
| 11083 | Inv-S Power | I16 | W | 1 |
| 11084 | Inv-T Power | I16 | W | 1 |
| 11085 | Grid-R Frequency | U16 | Hz | 100 |
| 11086 | Grid-S Frequency | U16 | Hz | 100 |
| 11087 | Grid-T Frequency | U16 | Hz | 100 |
| 11088 | Eps-R Voltage | U16 | V | 10 |
| 11089 | Eps-S Voltage | U16 | V | 10 |
| 11090 | Eps-T Voltage | U16 | V | 10 |
| 11091 | Eps-R Current | I16 | A | 10 |
| 11092 | Eps-S Current | I16 | A | 10 |
| 11093 | Eps-T Current | I16 | A | 10 |
| 11094 | Eps-R Power | I16 | W | 1 |
| 11095 | Eps-S Power | I16 | W | 1 |
| 11096 | Eps-T Power | I16 | W | 1 |
| 11097 | Eps Frequency | U16 | Hz | 100 |
| 11098 | Meter1-R Power | I16 | W | 1 |
| 11099 | Meter1-S Power | I16 | W | 1 |
| 11100 | Meter1-T Power | I16 | W | 1 |
| 11101 | Meter1 Power | I16 | W | 1 |
| 11102 | Meter2-R Power | I16 | W | 1 |
| 11103 | Meter2-S Power | I16 | W | 1 |
| 11104 | Meter2-T Power | I16 | W | 1 |
| 11105 | Meter2 Power | I16 | W | 1 |
| 11106 | Meter3-R Power | I16 | W | 1 |
| 11107 | Meter3-S Power | I16 | W | 1 |
| 11108 | Meter3-T Power | I16 | W | 1 |
| 11109 | Meter3 Power | I16 | W | 1 |
| 11110 | Load-R Power | I16 | W | 1 |
| 11111 | Load-S Power | I16 | W | 1 |
| 11112 | Load-T Power | I16 | W | 1 |
| 11113 | Load Power | I16 | W | 1 |

### 3.5 Energy counters — 14000–14026

All `RO`, unit `KWh`, gain `10`. `U32` rows occupy 2 registers, `U16` rows 1.

| Address | Signal Name | Type | Qty |
|---|---|---|---|
| 14000 | Total PV energy | U32 | 2 |
| 14002 | Today PV energy | U16 | 1 |
| 14003 | Total charge energy | U32 | 2 |
| 14005 | Today charge energy | U16 | 1 |
| 14006 | Total discharge energy | U32 | 2 |
| 14008 | Today discharge energy | U16 | 1 |
| 14009 | Total feed-in energy | U32 | 2 |
| 14011 | Today feed-in energy | U16 | 1 |
| 14012 | Total Consumption energy | U32 | 2 |
| 14014 | Today Consumption energy | U16 | 1 |
| 14015 | Total output energy | U32 | 2 |
| 14017 | Today output energy | U16 | 1 |
| 14018 | Total input energy | U32 | 2 |
| 14020 | Today input energy | U16 | 1 |
| 14021 | Total load energy | U32 | 2 |
| 14023 | Today load energy | U16 | 1 |
| 14024 | Total Eps energy | U32 | 2 |
| 14026 | Today Eps energy | U16 | 1 |

### 3.6 System settings — 20000–20013

All `RW`, `U16`, unit `N/A`, gain `1`, quantity `1`.

| Address | Signal Name | Scope |
|---|---|---|
| 20000 | System on/off | 0: off, 1: on |
| 20001 | RTC-year | 0-99 |
| 20002 | RTC-month | 1-12 |
| 20003 | RTC-day | 1-31 |
| 20004 | RTC-hour | 0-23 |
| 20005 | RTC-minute | 0-59 |
| 20006 | RTC-second | 0-59 |
| 20007 | Language | 0 English |
| 20008 | Grid code | *(no scope given)* |
| 20009 | Meter1 Addr | 1-247 (1) |
| 20010 | Meter2 Addr | 1-247 (2) |
| 20011 | Meter3 Addr | 1-247 (3) |
| 20012 | Modbus Addr | 1-247 |
| 20013 | Password | 0-9999 |

### 3.7 Protection enables — 20100–20109

All `RW`, `U16`, unit `N/A`, gain `1`, quantity `1`.
Scope for every row: **0: disable, 1: enable**.

| Address | Signal Name |
|---|---|
| 20100 | DRM0 state |
| 20101 | Meter1 En/Dis |
| 20102 | Remote power control Enable |
| 20103 | ISO En/Dis |
| 20104 | GFCI En/Dis |
| 20105 | Anti-islanding En/Dis |
| 20106 | Freq Protect Restrictive |
| 20107 | Ground connect check En/Dis |
| 20108 | Phase unbalance En/Dis |
| 20109 | Bypass Relay En/Dis |

### 3.8 Work mode and time-of-use — 21000–21021

All `RW`, quantity `1`.

| Address | Signal Name | Type | Unit | Gain | Scope |
|---|---|---|---|---|---|
| 21000 | Work mode | U16 | N/A | 1 | 0: Self use, 1: ForceTimeUse, 2: Back up, 3: Feed-in First |
| 21001 | P1GridCharge Flag | U16 | N/A | 1 | 0: disable, 1: enable |
| 21002 | ChargeStartTime1 Hour | U16 | N/A | 1 | 0-23 |
| 21003 | ChargeStartTime1 Minute | U16 | N/A | 1 | 0-59 |
| 21004 | ChargeEndTime1 Hour | U16 | N/A | 1 | 0-23 |
| 21005 | ChargeEndTime1 Minute | U16 | N/A | 1 | 0-59 |
| 21006 | P2GridCharge Flag | U16 | N/A | 1 | 0: disable, 1: enable |
| 21007 | ChargeStartTime2 Hour | U16 | N/A | 1 | 0-23 |
| 21008 | ChargeStartTime2 Minute | U16 | N/A | 1 | 0-59 |
| 21009 | ChargeEndTime2 Hour | U16 | N/A | 1 | 0-23 |
| 21010 | ChargeEndTime2 Minute | U16 | N/A | 1 | 0-59 |
| 21011 | BackUpGridCharge Flag | U16 | N/A | 1 | 0: disable, 1: enable |
| 21012 | BackupChargeStartTimeHour | U16 | N/A | 1 | 0-23 |
| 21013 | BackupChargeStartTimeMinute | U16 | N/A | 1 | 0-59 |
| 21014 | BackupChargeEndTimeHour | U16 | N/A | 1 | 0-23 |
| 21015 | BackupChargeEndTimeMinute | U16 | N/A | 1 | 0-59 |
| 21016 | Maximum set charge current | U16 | A | 10 | 0-30 |
| 21017 | Maximum set discharge current | U16 | A | 10 | 0-30 |
| 21018 | Minimum SoC | U16 | N/A | 1 | 5-100 % |
| 21019 | Minimum SoC-OnGrid | U16 | N/A | 1 | 5-100 % |
| 21020 | Export limit | U16 | W | 1 | 0-60000 |
| 21021 | Power limit percent | I16 | Pn/100 | 1 | [-100, 100] |

### 3.9 Connect / reconnect — 22000–22008

| Address | Signal Name | R/W | Type | Unit | Gain | Scope |
|---|---|---|---|---|---|---|
| 22000 | Con/Recon Enable | RO | Bitfield16 | N/A | 1 | Bit0: soft start, Bit1: voltage para, Bit2: freq para |
| 22001 | Connect time | RW | U16 | s | 1 | 10-600 |
| 22002 | Connect power gradient | RW | U16 | %/min | 10 | 6-3000 |
| 22003 | Reconnect time | RW | U16 | s | 1 | 10-600 |
| 22004 | Reconnect power gradient | RW | U16 | %/min | 10 | 6-3000 |
| 22005 | Connect voltage high limit | RW | U16 | V | 10 | 230-300 |
| 22006 | Connect voltage low limit | RW | U16 | V | 10 | 150-230 |
| 22007 | Connect frequency high limit | RW | U16 | Hz | 100 | 50-65 |
| 22008 | Connect frequency low limit | RW | U16 | Hz | 100 | 45-60 |

### 3.10 Voltage protection — 22100–22110

| Address | Signal Name | R/W | Type | Unit | Gain | Scope |
|---|---|---|---|---|---|---|
| 22100 | Voltage Protect Enable | RO | Bitfield16 | N/A | 1 | Bit0: level1 protect, Bit1: level2 protect, Bit2: 10min volt protect |
| 22101 | Voltage high limit1 | RW | U16 | V | 10 | 230-300 |
| 22102 | Voltage high limit1 protect time | RW | U16 | ms | 0.1 | 20-100000 ms |
| 22103 | Voltage high limit2 | RW | U16 | V | 10 | 230-300 |
| 22104 | Voltage high limit2 protect time | RW | U16 | ms | 0.1 | 20-100000 ms |
| 22105 | Voltage low limit1 | RW | U16 | V | 10 | 50-230 |
| 22106 | Voltage low limit1 protect time | RW | U16 | ms | 0.1 | 20-100000 ms |
| 22107 | Voltage low limit2 | RW | U16 | V | 10 | 50-230 |
| 22108 | Voltage low limit2 protect time | RW | U16 | ms | 0.1 | 20-100000 ms |
| 22109 | 10min voltage high limit | RW | U16 | V | 10 | 230-300 |
| 22110 | Voltage Protect Activate | RW | Bitfield16 | N/A | 1 | Bit0: level1 high, Bit1: level2 high, Bit2: level1 low, Bit3: level2 low, Bit4: 10min volt protect |

### 3.11 Frequency protection — 22200–22209

| Address | Signal Name | R/W | Type | Unit | Gain | Scope |
|---|---|---|---|---|---|---|
| 22200 | Frequency Protect Enable | RO | Bitfield16 | N/A | 1 | Bit0: level1 protect, Bit1: level2 protect |
| 22201 | Frequency high limit1 | RW | U16 | Hz | 100 | 50-65 |
| 22202 | Frequency high limit1 protect time | RW | U16 | ms | 0.1 | 20-100000 ms |
| 22203 | Frequency high limit2 | RW | U16 | Hz | 100 | 50-65 |
| 22204 | Frequency high limit2 protect time | RW | U16 | ms | 0.1 | 20-100000 ms |
| 22205 | Frequency low limit1 | RW | U16 | Hz | 100 | 45-60 |
| 22206 | Frequency low limit1 protect time | RW | U16 | ms | 0.1 | 20-100000 ms |
| 22207 | Frequency low limit2 | RW | U16 | Hz | 100 | 45-60 |
| 22208 | Frequency low limit2 protect time | RW | U16 | ms | 0.1 | 20-100000 ms |
| 22209 | Frequency Protect Activate | RW | Bitfield16 | N/A | 1 | Bit0: level1 high, Bit1: level2 high, Bit2: level1 low, Bit3: level2 low |

### 3.12 P(f) — active power vs frequency — 23000–23010

All `RW`, quantity `1`.

| Address | Signal Name | Type | Unit | Gain | Scope |
|---|---|---|---|---|---|
| 23000 | P(f) Enable | U16 | N/A | 1 | 0: disable, 1: enable |
| 23001 | Mode | U16 | N/A | 1 | 0: fstop deactivated, 1: fstop activated |
| 23002 | P(f) enter delay | U16 | ms | 1 | 0-5000 |
| 23003 | Overfrequency start frequency | U16 | Hz | 100 | 50-55 |
| 23004 | Overfrequency droop | U16 | N/A | 100 | 2 %-12 % |
| 23005 | Overfrequency recover frequency | U16 | Hz | 100 | 50-55 |
| 23006 | Underfrequency start frequency | U16 | Hz | 100 | 45-50 |
| 23007 | Underfrequency droop | U16 | N/A | 100 | 2 %-12 % |
| 23008 | Underfrequency recover frequency | U16 | Hz | 100 | 45-50 |
| 23009 | Power recover gradient | U16 | %/min | 10 | 6-3000 |
| 23010 | Power recover delay | U16 | s | 1 | 0-600 |

### 3.13 P(u) — active power vs voltage — 23100–23108

All `RW`, quantity `1`.

| Address | Signal Name | Type | Unit | Gain | Scope |
|---|---|---|---|---|---|
| 23100 | P(u) enable | U16 | N/A | 1 | 0: disable, 1: enable |
| 23101 | V1 | U16 | V | 10 | 150-300 |
| 23102 | V2 | U16 | V | 10 | 150-300 |
| 23103 | V3 | U16 | V | 10 | 150-300 |
| 23104 | V4 | U16 | V | 10 | 150-300 |
| 23105 | P1 | U16 | Pn/100 | 1 | 0-100 |
| 23106 | P4 | U16 | Pn/100 | 1 | 0-100 |
| 23107 | P(u) delay | U16 | s | 1 | 0-10 |
| 23108 | power decline gradient | U16 | (Pn/100)/s | 1 | 1-100 |

### 3.14 Reactive power — 23200–23223

All `RW`, quantity `1`.

The power-factor rows share one scope: **> 0: over-Excited [80,100]; < 0: under-Excited [-100,-80]**.
The Q(U) reactive rows share: **> 0: over-Excited [60,100]; < 0: under-Excited [-100,-60]**.

| Address | Signal Name | Type | Unit | Gain | Scope |
|---|---|---|---|---|---|
| 23200 | Reactive power control mode | U16 | N/A | 1 | 0: None, 1: Fixed PF, 2: Fixed Q, 3: CosphiP, 4: Q(U) |
| 23201 | Fixed PF | I16 | N/A | 100 | PF scope (above) |
| 23202 | Fixed Q | I16 | Var | 1 | > 0: over-Excited [0,3600]; < 0: under-Excited [-3600,0] |
| 23203 | CosphiP_Lockin V | U16 | V | 10 | 200-300 |
| 23204 | CosphiP_Lockout V | U16 | V | 10 | 200-300 |
| 23205 | CosphiP_PF1 | I16 | N/A | 100 | PF scope |
| 23206 | CosphiP_P1 | U16 | Pn/100 | 1 | 0-100 |
| 23207 | CosphiP_PF2 | I16 | N/A | 100 | PF scope |
| 23208 | CosphiP_P2 | U16 | Pn/100 | 1 | 0-100 |
| 23209 | CosphiP_PF3 | I16 | N/A | 100 | PF scope |
| 23210 | CosphiP_P3 | U16 | Pn/100 | 1 | 0-100 |
| 23211 | CosphiP_PF4 | I16 | N/A | 100 | PF scope |
| 23212 | CosphiP_P4 | U16 | Pn/100 | 1 | 0-100 |
| 23213 | Q(U) enter delay | U16 | s | 1 | 0-60 |
| 23214 | Q(U) LockinP | U16 | Pn/100 | 1 | 0-100 |
| 23215 | Q(U) LockoutP | U16 | Pn/100 | 1 | 0-100 |
| 23216 | Qu_V1 | U16 | Un/100 | 1 | 0-130 |
| 23217 | Qu_Q1 | I16 | Pn/100 | 100 | Q(U) scope (above) |
| 23218 | Qu_V2 | U16 | Un/100 | 1 | 0-130 |
| 23219 | Qu_Q2 | I16 | Pn/100 | 100 | Q(U) scope |
| 23220 | Qu_V3 | U16 | Un/100 | 1 | 0-130 |
| 23221 | Qu_Q3 | I16 | Pn/100 | 100 | Q(U) scope |
| 23222 | Qu_V4 | U16 | Un/100 | 1 | 0-130 |
| 23223 | Qu_Q4 | I16 | Pn/100 | 100 | Q(U) scope |

### 3.15 Ride-through — 23300–23315

All `RW`, `U16`, quantity `1`.

| Address | Signal Name | Unit | Gain | Scope |
|---|---|---|---|---|
| 23300 | UVRT enable | N/A | 1 | 0: disable, 1: enable |
| 23301 | OVRT enable | N/A | 1 | 0: disable, 1: enable |
| 23302 | UVRT V1 | Un/100 | 1 | 0-100 |
| 23303 | UVRT T1 | ms | 0.1 | 10-60000 ms |
| 23304 | UVRT V2 | Un/100 | 1 | 0-100 |
| 23305 | UVRT T2 | ms | 0.1 | 10-60000 ms |
| 23306 | UVRT V3 | Un/100 | 1 | 0-100 |
| 23307 | UVRT T3 | ms | 0.1 | 10-60000 ms |
| 23308 | OVRT V1 | Un/100 | 1 | 100-130 |
| 23309 | OVRT T1 | ms | 0.1 | 10-60000 ms |
| 23310 | OVRT V2 | Un/100 | 1 | 100-130 |
| 23311 | OVRT T2 | ms | 0.1 | 10-60000 ms |
| 23312 | OVRT V3 | Un/100 | 1 | 100-130 |
| 23313 | OVRT T3 | ms | 0.1 | 10-60000 ms |
| 23314 | Zero current under voltage threshold | Un/100 | 1 | 20-100 |
| 23315 | Zero current over voltage threshold | Un/100 | 1 | 100-130 |

### 3.16 ROCOF — 23400–23401

| Address | Signal Name | R/W | Type | Unit | Gain | Scope |
|---|---|---|---|---|---|---|
| 23400 | ROCOF enable | RW | U16 | N/A | 1 | 0: disable, 1: enable |
| 23401 | ROCOF threshold | RW | U16 | Hz/s | 10 | 0.1-5 |

### 3.17 Commands — 24000–24004

Write-only. `24000`–`24003` have no type, unit or gain in the document.

| Address | Signal Name | R/W | Type | Unit | Gain | Qty | Scope |
|---|---|---|---|---|---|---|---|
| 24000 | Clear events | WO | | | | 1 | |
| 24001 | Clear energy | WO | | | | 1 | |
| 24002 | Factory reset | WO | | | | 1 | |
| 24003 | Battery awake | WO | | | | 1 | |
| 24004 | Start Self-Test | WO | U16 | N/A | 1 | 1 | 0: Start |

The document ends here. There is nothing above 24004.

---

## Transcription notes

Extracted with `pdftotext -table` (Xpdf 4.00) and read row by row. Points where
the source is ambiguous or incomplete, recorded rather than guessed:

* **Temperature units are blank.** Registers 11016–11019, 11025, 11036–11037 and
  11046–11047 have an empty Unit cell, and no degree glyph appears anywhere in
  the extracted text. Almost certainly °C at gain 10, but the document as
  extracted does not say so.
* **Fault bits are undefined.** 11060–11075 are typed `Bitfield16` with no bit
  meanings anywhere in the document.
* **Grid code (20008) has no scope.** No enum of grid codes is given.
* **Language (20007) lists only `0 English`.** No other values are printed.
* **`Bitfield16` wraps as `Bitfie` / `ld16`** in the raw extraction; joined here.
* A plain `pdftotext -layout` pass mis-assigns addresses around 11058–11062
  through column collision, shifting the Fault block by two. The `-table` output
  is correct there and agrees with the app's own decode (Fault 1 = 11060). If you
  re-extract, use `-table`.

---

# Part B — this unit, the web page, findings and guesses

## 4. Labels, sources, the unit

Every statement below carries one of three labels:

- **Measured** — read from the inverter; the bytes are in the sources below.
- **Confirmed** — two independent sources agree: the inverter's value and
  Renac's web page, or the inverter and the app, or two register sets.
- **Guess** — not proven. The evidence is given, and how it could be settled.
  Section 8 lists every guess in one place.

The unit: model `N3-HV-5.0`, its serial not published, firmware master
1.21, slave 1.00, HMI 2.03, protocol version 0.39 (the document is V1.00), grid
code Greece, **no battery** (as of 2026-10-14). Module HF-LPT270, firmware
`LFWIFI202406060000`.

| Source | What | When |
|---|---|---|
| captures 1 and 2, `conversation.yml`, `conversation2.raw` | the cloud's sessions, captured | 2026-09-18, night, idle |
| capture 3, `conversation3.raw` | the same, inverter generating | 2026-09-19 10:53 |
| "the relay run" | `tools/cloud_relay.py --live` between the module and the cloud, its log: one connection, 12 polls 5 min apart | 2026-10-13 23:29 → 2026-10-14 00:28, night, idle |
| `conversation4.raw` | the relay run's first poll plus the keepalive, each reply as the cloud got it | from that run's log |
| Renac web portal, device page, tab "Para setting", after pressing every Read | labels, ranges, values | 2026-10-14 00:49 |
| [PROTOCOL.md](PROTOCOL.md) §5, §7, §8 | the app's decode and the first capture analysis | 2026-09 |

The captures and the log are not in this repo: they hold the unit's serials
and network addresses.

**Measured: between captures 2 and 3 and the relay run, 24 days apart, only the
live values (11000 block, 11124 block), the energy counters (14000 block) and
the clock (20001–20006) changed.** Every other reply, settings, protection,
identity and every undocumented register below included, is byte-identical in
all three.

## 5. The document's registers, checked

### 5.1 Against the web page

The "Para setting" tab of Renac's portal, after Read, against the registers in
the relay run. The page's Read does not send a Modbus request at once: the
cloud answers it from its own copy and polls the inverter on its own schedule
(tried several times on 2026-10-14).

| Page section and label | Register | Raw value | Page shows | Result |
|---|---|---|---|---|
| Basic, System switch | 20000 | 1 | ON | confirmed |
| Basic, Date&Time | 20001–20006 | the clock | local date and time | confirmed (the cloud writes it, 5.3) |
| Basic, Language | 20007 | 0 | English | confirmed |
| Basic, Country regulation | 20008 | 6 | Greece | **confirmed: grid code 6 = Greece**; the document has no grid code list |
| Basic, Meter1/2/3 address[1,247] | 20009–20011 | 1, 1, 1 | 1, 1, 1 | confirmed |
| Basic, Modbus address[1,247] | 20012 | 1 | 1 | confirmed |
| Basic, Password[0,9999] | 20013 | 0 | 1 | the page most likely hides the real value |
| Basic, DRM0 state | 20100 | 0 | Disabled | confirmed |
| Basic, Meter1 En/Dis | 20101 | 1 | Enable | confirmed |
| Basic, Phase unbalance En/Dis | 20108 | 0 | Disabled | confirmed |
| Basic, Bypass relay En/Dis | 20109 | 0 | Disabled | confirmed |
| Working mode | 21000 | 0 | Self use | confirmed |
| Function, Max charge / discharge current[0,50](A) | 21016, 21017 | 300, 300 | 30, 30 | gain 10 confirmed; **the page's range is 0–50 A, the document's 0–30** |
| Function, Min SOC / Min SOC-OnGrid | 21018, 21019 | 5, 5 | 5, 5 | confirmed |
| Function, Export limit | 21020 | 0 | 0 | confirmed |
| Function, Power limit percent | 21021 | 100 | 100 | confirmed |
| Safety connect, Connect / Reconnect time | 22001, 22003 | 10, 10 | 10, 10 s | confirmed |
| Safety connect, Connect / Reconnect power gradient | 22002, 22004 | 6000, 6000 | 600, 600 %/min | gain 10 confirmed |
| Safety connect, Voltage high / low limit | 22005, 22006 | 2530, 2070 | 253, 207 V | confirmed |
| Safety connect, Frequency high / low limit | 22007, 22008 | 5010, 4990 | 50.1, 49.9 Hz | confirmed |
| Safety voltage, Voltage high limit1 / limit2 | 22101, 22103 | 2645, 2645 | 264.5 V | confirmed |
| Safety voltage, Voltage low limit1 / limit2 | 22105, 22107 | 1840, 1840 | 184 V | confirmed |
| Safety voltage and frequency, the eight protect times | 22102…22108, 22202…22208 | 50 each | 500 ms | gain 0.1 confirmed: ms = raw / 0.1 |
| Safety voltage, Voltage high 10 min | 22109 | 2530 | 253 V | confirmed |
| Safety voltage, Voltage protect activate | 22110 | 31 | all five ticked | confirmed bit for bit |
| Safety frequency, Frequency high limit1 / limit2 | 22201, 22203 | 5050, 5050 | 50.5 Hz | confirmed |
| Safety frequency, Frequency low limit1 / limit2 | 22205, 22207 | 4950, 4950 | 49.5 Hz | confirmed |
| Safety frequency, Frequency protect activate | 22209 | 15 | all four ticked | confirmed |
| Con/Recon enable, Voltage protect enable, Frequency protect enable | 22000, 22100, 22200 (RO) | 7, 5, 1 | every box empty | the page does not show these read-only bitfields |
| Command, Clear events … Start self-Test | 24000–24004 | — | buttons | names match; not tried |

Documented registers the page does not show: 20102–20107 (remote power
control, ISO, GFCI, anti-islanding, restrictive frequency protection, ground
check), 21001–21015 (charge windows; probably the "Intelligent Mode" tab, not
looked at) and 23000–23401 (P(f), P(u), reactive power, ride-through, ROCOF).
The page settings that have no documented register are in section 7.

### 5.2 Against the app

From [PROTOCOL.md](PROTOCOL.md) §8, where the document is right and the app is
wrong:

- 11094–11096 Eps power are I16, signed; the app reads them unsigned.
- 11088–11090 Eps voltage are U16; the app reads them signed (no visible effect
  below 3276.7 V).
- 11040 bit 3 is the forced charge request and bit 4 the balance charge
  request; the app looks at bits 2–0 only.

Measured: 10181 Protocol Ver reads 39 (0.39) while the document is V1.00.

### 5.3 How the cloud uses them

- **Clock, 20001–20006** — measured: the cloud writes it once per connection,
  in its first poll, with fn 16, and reads it back; one write in the relay
  run's 12 polls, none at midnight. It writes the plant's local time (UTC+3
  for this unit). How it picks the time zone is guess G10.
- **11121 × 3** — measured: the cloud's request always carries a bad CRC
  (`9D F4`, correct `5C 34`), so 11121–11123 are never read in production.
- **Identity 10000 × 32** — measured: read in the first poll of a connection
  only. The full list of reads is in `conversation4.raw`; PROTOCOL.md §7.2 has
  the first capture's.
- **Later polls on the same connection** — measured, every 5 minutes: no
  `AA 55` handshake but a single `0x15 / 0x0B` in the middle, no `10000 × 32`,
  no clock write; 47 reads. After the bad-CRC `11121 × 3` the cloud waits 10 s,
  then sends `11011 × 1` and `10618 × 2`, which close every poll. Between polls
  it sends the `0x15 / 0x02` keepalive, unanswered. The list of reads is the
  one of capture 3, byte for byte, 24 days later.

### 5.4 Values of this unit without a battery

The baseline to compare with once the battery is in (section 9). Raw words from
`conversation4.raw`, night, inverter idle; all identical in captures 2 and 3
except where noted.

| Registers | Raw words |
|---|---|
| 10032–10175 BMS serials | all 0 |
| 10176–10181 versions | 121, 100, 203, 0, 0, 39 |
| 10200–10215 self-test results | all 0 (the self-test has presumably never run) |
| 11020–11056 battery and BMS | 11020 = 6 (0.6 V), all the rest 0; generating (capture 3) 11020 = 120 (12.0 V), 11022 = −3 W |
| 11057 inverter state | 0 Wait at night; 2 Normal generating (capture 3) |
| 11058, 11059 connect states | BMS 0 (NG), Meter1 1 (OK) |
| 14003–14008 charge / discharge energy | all 0 |
| 20000–20013 | 1; clock; 0, 6, 1, 1, 1, 1, 0 |
| 20100–20113 | 0, 1, 0, 1, 1, 1, 0, 0, 0, 0, then outside the document 1, 0, 0, 254 |
| 21000, 21016–21021 | 0; 300, 300, 5, 5, 0, 100 |
| 22000–22008 | 7, 10, 6000, 10, 6000, 2530, 2070, 5010, 4990 |
| 22100–22110 | 5, 2645, 50, 2645, 50, 1840, 50, 1840, 50, 2530, 31 |
| 22200–22209 | 1, 5050, 50, 5050, 50, 4950, 50, 4950, 50, 15 |
| 23000–23010 | 0, 0, 10, 5020, 5, 5020, 4970, 5, 4980, 60, 1 |
| 23100–23108 | 0, 1950, 2150, 2530, 2580, 0, 0, 1, 100 |
| 23200–23223 | 0, 100, 0, 2415, 2254, 100, 50, 90, 100, 0, 0, 0, 0, 1, 20, 5, 93, 440, 97, 0, 103, 0, 107, 440 |
| 23300–23315 | 0, 0, 70, 270, 30, 130, 0, 30, 125, 150, 120, 150, 115, 150, 85, 110 |
| 23400–23401 | 0, 5 |

The undocumented registers' values are in section 6.

## 6. Registers the document does not have

All measured on this unit; meanings are guesses where marked.

| Registers | The cloud reads | Value here | What we know |
|---|---|---|---|
| 10183–10185 | 10183 × 3 | 0, 0, 0 | nothing; the document's version block ends at 10182 (HMI Boot Ver), which nobody reads |
| 10500–10563 | 10500 × 64 | exception 02 | guess G6: the cloud probing for another device family |
| 10600–10617, 10618–10619 | 10600 × 18, 10618 × 2 | exception 02 | guess G6, same probe |
| 11114–11120 | 11114 × 7 | 121, 100, 203, 0, 0, 0, 0 | 11114–11116 equal 10176–10178, the inverter's firmware versions, in every session (G8). 11119 is 0 where 10181 is 39, so it is not a copy of the whole version block. 11117–11120 unknown |
| 11121–11123 | 11121 × 3, bad CRC | never answered | 5.3 |
| 11124–11157 | 11124 × 34 | live | **confirmed** (capture 3, generating): I32 copies of the power registers, table below |
| 20110–20113 | inside 20100 × 14 | 1, 0, 0, 254 | settings range (G5); candidates for section 7 |
| 20114–20116 | × 1 each | 254, 0, 0 | settings range (G5); candidates for section 7 |
| 20117–20121 | — | never read | — |
| 20122 | × 1 | 1 | settings range (G5); candidate for section 7 |
| 20123–20124 | — | never read | — |
| 20125–20129 | 20125 × 5 | 0 × 5 | settings range (G5); candidates for section 7 |
| 20130 | × 1 | 4200 | guess G1: the page's "RCR EnWGl4a Limit Power[0,4200]" |
| 20131–20151 | 20131 × 21 | 0 × 21 | settings range (G5); candidates for section 7 |
| 21022 | × 1 | 0 | the document's work-mode block ends at 21021 |
| 23109 | inside 23100 × 10 | 5 | the document's P(u) block ends at 23108 |
| 23224–23225 | inside 23200 × 26 | 5, 40 | the document's reactive power block ends at 23223 |
| 23316 | inside 23300 × 17 | 0 | the document's ride-through block ends at 23315 |
| 23620–23626 | 23620 × 7 | not data | guess G7: a comms buffer, layout below |
| 23640–23646, 23660–23666 | × 7 each | exception 02 | — |
| 50028–50030 | × 1 each | 0, 12, 0 | never changed in any session; PROTOCOL.md §7.6 called it a live window (G11) |
| 50031, 50032, 50035 | × 1 each | exception 02 | — |

**11124–11157, I32 copies of the power registers** (confirmed in capture 3,
PROTOCOL.md §7.6): big-endian signed 32-bit, two registers per field.

| I32 | Field | Capture 3 | I16 source | Capture 3 |
|---|---|---|---|---|
| 11124 | battery power | −3 | 11022 | −3 |
| 11126 / 11128 / 11130 / 11132 | Meter1 R / S / T / total | 437 / −189 / −243 / 4 | 11098–11101 | 436 / −191 / −243 / 1 |
| 11134–11140 | Meter2 R / S / T / total | 0 | 11102–11105 | 0 |
| 11142–11148 | Meter3 R / S / T / total | 0 | 11106–11109 | 0 |
| 11150 / 11152 / 11154 / 11156 | Load R / S / T / total | 741 / 38 / 41 / 820 | 11110–11113 | 740 / 37 / 42 / 819 |

They carry the sign without doubt and cannot clip at ±32767 W.

**23620 × 7, the reply byte by byte** (measured; the reading is guess G7):

```
request   01 03 5C 44 00 07 56 4D                       23620 × 7
before    01 03 0A 00 00 00 00 00 00 00 00 00 00 24 B6  reply to 20125 × 5, sent just before
reply     01 03 0E 44 00 07 56 4D 00 00 00 00 00 24 B6 00 00 39 2B
buffer       0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16
```

Buffer bytes 3–7 are the request's own bytes, bytes 8–14 the tail of the
previous reply, its CRC `24 B6` included, bytes 15–16 zero.

## 7. Web page settings without a documented register

The "Para setting" tab shows these, and no register of the document matches
them. Values after Read on 2026-10-14. **The "candidates" column is guessing
only**: it lists the undocumented registers whose value fits the page's value,
nothing more.

| # | Page section, label | Control | Page value | Candidates (guess) |
|---|---|---|---|---|
| 1 | Basic, AFCI En/Dis | dropdown | Disabled | one of the zeros |
| 2 | Basic, AFCI_sensitivity[1,10] | number | 0, outside its own range | one of the zeros |
| 3 | Basic, AFCI self-test | dropdown | Not self-checked | one of the zeros |
| 4 | Basic, Meter2 En/Dis | dropdown | Disabled | one of the zeros; the document has only Meter1 En/Dis (20101) |
| 5 | Basic, Meter3 En/Dis | dropdown | Disabled | one of the zeros |
| 6 | Basic, Battery model | dropdown | No Battery | 20113 or 20114 (254), or one of the zeros — G3 |
| 7 | Basic, RCR En/Dis | dropdown | Disabled | one of the zeros |
| 8 | Basic, RCR EnWGl4a Limit Power[0,4200] | number | 4200 | **20130 = 4200** — G1 |
| 9 | Basic, Parallel master | dropdown | Single | 20113 or 20114 (254), or one of the zeros — G3 |
| 10 | Basic, EPS mode | dropdown | Enable | 20110 or 20122, the only undocumented 1s — G2 |
| 11 | Basic, Battery awake | dropdown + Save | Disabled | one of the zeros; 24003 is a command, and the Command section has its own button for it |
| 12 | Command, AFCI faulty reset | button | — | a command; reading cannot find it |
| 13 | Command, AFCI factorySet | button | — | a command; reading cannot find it |
| 14 | Advanced, Dry contact | dropdown | Disabled | one of the zeros |

"The zeros": 20111, 20112, 20115, 20116, 20125–20129, 20131–20151, 21022,
50028, 50030. Left over with nothing on the page: 50029 = 12, and 23109,
23224–23225, 23316 in the protection blocks.

## 8. Guesses

Each is unproven. How to settle G1–G4: the write test in demo mode. The mock
module (`tests/mocks/mock_hf_lpt270.py --tcp`) carries Renac's demo plant
identity; `cloud_relay.py` between it and the cloud blocks every write outside
the clock and logs its register and value. Press Save on one field of the demo
plant's page at a time; each logged write names the field's register. Not with
the real inverter.

| # | Guess | Evidence | How to settle |
|---|---|---|---|
| G1 | 20130 is "RCR EnWGl4a Limit Power[0,4200]", in W | the only register holding 4200; the page shows 4200 and its range ends at 4200 | demo write test |
| G2 | "EPS mode" is 20110 or 20122 | the page shows Enable; those are the only undocumented registers holding 1 | demo write test |
| G3 | "Battery model" (No Battery) and/or "Parallel master" (Single) are 20113 / 20114 | both hold 254 (0xFE), which looks like a "none" marker | demo write test; the battery (section 9) |
| G4 | the other page settings of section 7 are among the zeros | the page shows Disabled or 0 for all of them; nothing tells them apart | demo write test |
| G5 | 20110–20151 and 21022 are read-write settings | they sit inside the settings range (20000–21021 are RW) and never changed in 24 days | a write accepted by the inverter, or a value change made on the page |
| G6 | 10500 × 64, 10600 × 18 and 10618 × 2 are the cloud probing for another device family | 10500 and 10600 are the app's `Renac10` blocks (PROTOCOL.md §3, §7.2); 10618 follows 10600 × 18 directly; all three get exception 02 | — |
| G7 | 23620 × 7 shows a comms buffer, not registers: the inverter writes the reply header over the request in one buffer and fills no data | every byte of the reply is accounted for (section 6); the cloud always reads 20125 × 5 just before it, so the reply never changes | read 23620 × 7 after a different frame: the bytes should follow that frame |
| G8 | 11114–11116 are a copy of the firmware versions 10176–10178 | equal in every session | a firmware update: both should change together |
| G9 | Q(U) 23217 (Qu_Q1) and 23223 (Qu_Q4) = 440 use gain 10 (44.0 % of Pn), not the document's 100 | 440 fits neither the document's gain (4.4) nor its scope (60–100); 44 % is the reactive share at PF 0.90 (sin(acos 0.90) = 0.436), and the CosphiP curve here goes down to PF 0.90 (23207 = 90, gain 100) | the page's reactive power settings, if it has them |
| G10 | the cloud takes the clock's time zone from the plant the inverter serial is registered to | it wrote UTC+3 for this unit, and UTC+8 behind the mock carrying Renac's demo plant identity (relay log, 2026-10-13 22:25) | — |
| G11 | 50028–50030 is a live window | PROTOCOL.md §7.6 says so; nothing has moved it in any session | a session with the battery, or generating with load |

## 9. With the battery: what to compare

When the battery module is installed: run the relay the same way
(`cloud_relay.py --live`, socket B pointed at it), build a new fixture the way
`conversation4.raw` was built (the first poll after the cloud connects plus the
requests later polls add, each with the reply the cloud got) and compare it
with `conversation4.raw` and section 5.4. Also read the "Para setting" tab
again.

Expected to change (guesses):

- 10032–10175: BMS serials, text instead of zeros; 10179–10180 BMS versions.
- 11020–11057: battery and BMS live values; 11058 BMS connect state 0 → 1;
  11124 battery power (I32).
- 14003–14008: charge and discharge energy.
- The page's "Battery model": the register that changes with it settles part of
  G3; "Battery awake" may move too.
- Possibly 20113 / 20114 (254), 50028–50030 (G11), 21016–21019.

Anything else that changes is new information. Compare the cloud's list of
reads too: with a battery it may read more.
