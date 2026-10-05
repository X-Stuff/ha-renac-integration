"""The register map of the Renac N3-HV, as the vendor document V1.00 and the app define it.

Where the app and the document disagree, the document wins: 11088-11090 EPS voltage is unsigned, 11094-11096 EPS
power is signed, 11040 bits 3 and 4 are the request flags. The power values come from the documented 16-bit
registers; the undocumented 32-bit block at 11124 (`POWER_32BIT`), verified against them on this unit, carries the
same values under the same keys: the power32 group (groups.py) reads it, and its readings stand in for the 16-bit
ones while that read succeeds. It may not exist on other models.
"""

from __future__ import annotations

from .registers import PHASES, Kind, Register

DAY_S = 24 * 3600.0
HOUR_S = 3600.0
MINUTE_S = 60.0
CLOCK_SECONDS_MAX_AGE_S = 5.0

INVERTER_STATES = {
    0: "Wait",
    1: "Check",
    2: "Normal",
    3: "Standby",
    4: "Fault",
    5: "Permanent",
    6: "Update",
    7: "EPS check",
    8: "EPS",
    9: "Self test",
    10: "Idle",
}

BATTERY_MODES = {0: "Sleep", 1: "Charge", 2: "Discharge", 3: "Idle"}

WORK_MODES = {0: "Self use", 1: "Force time use", 2: "Back up", 3: "Feed-in first"}

REACTIVE_MODES = {0: "None", 1: "Fixed PF", 2: "Fixed Q", 3: "Cos phi (P)", 4: "Q(U)"}

CONNECTED = {0: "NG", 1: "OK"}
ON_OFF = {0: "off", 1: "on"}
ENABLED = {0: "disabled", 1: "enabled"}

# 11040 BMS basic status: bits 2-0 are the mode, bit 3 forced charge request, bit 4 balance charge request.
BATTERY_STATUS_MODE_MASK = 0x07
BATTERY_STATUS_FORCED_CHARGE_BIT = 3
BATTERY_STATUS_BALANCE_CHARGE_BIT = 4

# Fault bit names from the app, in plain bit order; only six of the sixteen words have names.
FAULT_1_BITS = (
    "HardwareTrip",
    "MainsLost",
    "GridVoltFault",
    "GridFreqFault",
    "PvVoltFault",
    "BusVoltFault",
    "BatteryVoltFault",
    "Ac10MinsVoltFault",
    "Dci_OCP",
    "Dcv_OVP",
    "SW_OCP",
    "RC_OCP",
    "IsolationFault",
    "TemperatureOverFault",
    "BatteryConnDirFault",
    "SampleConsistenceFault",
)
FAULT_2_BITS = (
    "EpsOverLoad",
    "POverLoad",
    "InputConfigFault",
    "BatLowFault",
    "",
    "",
    "",
    "BMS_Lost",
    "InternalCommsFault",
    "Fan_DeviceFault",
    "HCT_DeviceFault",
    "EepromFault",
    "RCD_Dev_Fault",
    "EpsRelayFault",
    "GridRelayFault",
    "Other_DeviceFault",
)
FAULT_7_BITS = ("HmiEepromFault", "SciCommsFault", "Meter_Lost")
FAULT_8_BITS = (
    "BMS_Fault_VOLT",
    "BMS_Fault_TEPR",
    "BMS_Fault_IN_COM",
    "BMS_Fault_DCOV",
    "BMS_Fault_RV",
    "BMS_Fault_RELAY",
    "BMS_Fault_CELL",
    "BMS_Fault_OTHER",
)
FAULT_9_BITS = tuple(
    "BMS_Protect_" + name
    for name in (
        "PRE_CHG",
        "CHG_OV",
        "DSG_UV",
        "CHG_OT",
        "CHG_UT",
        "DSG_OT",
        "DSG_UT",
        "DSG_OC",
        "CHG_OC",
        "OCD",
        "DSG_USOC",
        "OFFLINE",
        "OVD",
        "OTD",
        "OSOP",
        "NC_FAULT",
    )
)
FAULT_10_BITS = tuple("BMS_Alarm_" + name[len("BMS_Protect_") :] for name in FAULT_9_BITS)

FAULT_BIT_NAMES = {11060: FAULT_1_BITS, 11061: FAULT_2_BITS, 11066: FAULT_7_BITS, 11067: FAULT_8_BITS, 11068: FAULT_9_BITS, 11069: FAULT_10_BITS}


def battery_status_text(word: int) -> str:
    """The battery status word as text: the mode and the request flags that are set."""
    text = BATTERY_MODES.get(word & BATTERY_STATUS_MODE_MASK, f"reserved ({word & BATTERY_STATUS_MODE_MASK})")
    if word & (1 << BATTERY_STATUS_FORCED_CHARGE_BIT):
        text += ", forced charge request"
    if word & (1 << BATTERY_STATUS_BALANCE_CHARGE_BIT):
        text += ", balance charge request"
    return text


def _phases(address: int, key: str, name: str, kind: Kind, scale: int, unit: str, **extra: object) -> list[Register]:
    """Three registers, one per phase, at consecutive addresses."""
    return [Register(address + i, f"{key}_{phase}", f"{name} {phase.upper()}", kind, scale, unit, **extra) for i, phase in enumerate(PHASES)]


def _phase_powers(address: int, key: str, name: str, kind: Kind, **extra: object) -> list[Register]:
    """Three power registers, one per phase, and their sum under the plain key and name, consecutive: one word each as
    I16, two as I32."""
    stride = 2 if kind is Kind.I32 else 1
    registers = [Register(address + stride * i, f"{key}_{phase}", f"{name} {phase.upper()}", kind, 1, "W", **extra) for i, phase in enumerate(PHASES)]
    registers.append(Register(address + stride * 3, key, name, kind, 1, "W", **extra))
    return registers


IDENTITY: tuple[Register, ...] = (
    Register(10000, "model", "Model", Kind.TEXT),
    Register(10016, "serial", "Inverter serial", Kind.TEXT),
    Register(10176, "master_version", "Inverter master version", Kind.VERSION),
    Register(10177, "slave_version", "Inverter slave version", Kind.VERSION),
    Register(10178, "hmi_version", "Inverter HMI version", Kind.VERSION),
    Register(10179, "bms_master_version", "BMS master version", Kind.VERSION, diagnostic=True),
    Register(10180, "bms_slave_version", "BMS slave version", Kind.VERSION, diagnostic=True),
    Register(10181, "protocol_version", "Protocol version", Kind.VERSION),
)

LIVE: tuple[Register, ...] = (
    Register(11000, "pv1_voltage", "PV1 voltage", Kind.I16, 10, "V"),
    Register(11001, "pv1_current", "PV1 current", Kind.I16, 10, "A"),
    Register(11002, "pv1_power", "PV1 power", Kind.I16, 1, "W"),
    Register(11003, "pv2_voltage", "PV2 voltage", Kind.I16, 10, "V"),
    Register(11004, "pv2_current", "PV2 current", Kind.I16, 10, "A"),
    Register(11005, "pv2_power", "PV2 power", Kind.I16, 1, "W"),
    Register(11016, "internal_temperature", "Internal temperature", Kind.I16, 10, "°C"),
    Register(11017, "charger_temperature", "Charger temperature", Kind.I16, 10, "°C"),
    Register(11018, "boost_temperature", "Boost temperature", Kind.I16, 10, "°C"),
    Register(11019, "inverter_temperature", "Inverter temperature", Kind.I16, 10, "°C"),
    Register(11020, "battery_voltage", "Battery voltage", Kind.I16, 10, "V"),
    Register(11021, "battery_current", "Battery current", Kind.I16, 10, "A"),
    Register(11022, "battery_power", "Battery power", Kind.I16, 1, "W"),
    Register(11023, "bms_voltage", "BMS voltage", Kind.I16, 10, "V"),
    Register(11024, "bms_current", "BMS current", Kind.I16, 10, "A"),
    Register(11025, "bms_temperature", "BMS temperature", Kind.I16, 10, "°C"),
    Register(11026, "soc", "Battery SoC", Kind.U16, 1, "%"),
    Register(11027, "soh", "Battery SoH", Kind.U16, 1, "%"),
    Register(11028, "bms_charge_cutoff_voltage", "BMS charge cutoff voltage", Kind.I16, 10, "V", diagnostic=True, disabled=True),
    Register(11029, "bms_discharge_cutoff_voltage", "BMS discharge cutoff voltage", Kind.I16, 10, "V", diagnostic=True, disabled=True),
    Register(11030, "bms_max_charge_current", "BMS max charge current", Kind.I16, 10, "A", diagnostic=True, disabled=True),
    Register(11031, "bms_max_discharge_current", "BMS max discharge current", Kind.I16, 10, "A", diagnostic=True, disabled=True),
    Register(11032, "bms_max_cell_voltage", "BMS max cell voltage", Kind.I16, 1000, "V", diagnostic=True, disabled=True),
    Register(11033, "bms_min_cell_voltage", "BMS min cell voltage", Kind.I16, 1000, "V", diagnostic=True, disabled=True),
    Register(11034, "bms_max_cell_voltage_index", "BMS max cell voltage index", Kind.U16, diagnostic=True, disabled=True),
    Register(11035, "bms_min_cell_voltage_index", "BMS min cell voltage index", Kind.U16, diagnostic=True, disabled=True),
    Register(11036, "bms_max_cell_temperature", "BMS max cell temperature", Kind.I16, 10, "°C", diagnostic=True, disabled=True),
    Register(11037, "bms_min_cell_temperature", "BMS min cell temperature", Kind.I16, 10, "°C", diagnostic=True, disabled=True),
    Register(11038, "bms_max_cell_temperature_index", "BMS max cell temperature index", Kind.U16, diagnostic=True, disabled=True),
    Register(11039, "bms_min_cell_temperature_index", "BMS min cell temperature index", Kind.U16, diagnostic=True, disabled=True),
    Register(11040, "battery_status", "Battery status", Kind.BITS, text_of=battery_status_text, diagnostic=True),
    Register(11041, "battery_cycles", "Battery cycle count", Kind.U16),
    Register(11042, "bms_max_module_voltage", "BMS max module voltage", Kind.I16, 10, "V", diagnostic=True, disabled=True),
    Register(11043, "bms_min_module_voltage", "BMS min module voltage", Kind.I16, 10, "V", diagnostic=True, disabled=True),
    Register(11044, "bms_max_module_voltage_index", "BMS max module voltage index", Kind.U16, diagnostic=True, disabled=True),
    Register(11045, "bms_min_module_voltage_index", "BMS min module voltage index", Kind.U16, diagnostic=True, disabled=True),
    Register(11046, "bms_max_module_temperature", "BMS max module temperature", Kind.I16, 10, "°C", diagnostic=True, disabled=True),
    Register(11047, "bms_min_module_temperature", "BMS min module temperature", Kind.I16, 10, "°C", diagnostic=True, disabled=True),
    Register(11048, "bms_max_module_temperature_index", "BMS max module temperature index", Kind.U16, diagnostic=True, disabled=True),
    Register(11049, "bms_min_module_temperature_index", "BMS min module temperature index", Kind.U16, diagnostic=True, disabled=True),
    Register(11050, "charge_forbidden", "Charging forbidden", Kind.U16, diagnostic=True),
    Register(11051, "discharge_forbidden", "Discharging forbidden", Kind.U16, diagnostic=True),
    Register(11052, "cell_count", "Battery cell count", Kind.U16, diagnostic=True, disabled=True),
    Register(11053, "module_count", "Battery module count", Kind.U16, diagnostic=True, disabled=True),
    Register(11054, "cells_per_module", "Cells per module", Kind.U16, diagnostic=True, disabled=True),
    Register(11055, "voltage_level", "Battery voltage level", Kind.U16, 1, "V", diagnostic=True, disabled=True),
    Register(11056, "battery_capacity", "Battery capacity", Kind.U16, 1, "Ah"),
    Register(11057, "inverter_state", "Inverter state", Kind.ENUM, enum=INVERTER_STATES, diagnostic=True),
    Register(11058, "bms_connected", "BMS connected", Kind.ENUM, enum=CONNECTED, diagnostic=True),
    Register(11059, "meter_connected", "Meter 1 connected", Kind.ENUM, enum=CONNECTED, diagnostic=True),
    *[Register(11060 + i, f"fault_{i + 1}", f"Fault word {i + 1}", Kind.BITS, bit_names=FAULT_BIT_NAMES.get(11060 + i), diagnostic=True) for i in range(16)],
    *_phases(11076, "grid_voltage", "Grid voltage", Kind.U16, 10, "V"),
    *_phases(11079, "inverter_current", "Inverter current", Kind.I16, 10, "A"),
    *_phases(11082, "inverter_power", "Inverter power", Kind.I16, 1, "W"),
    *_phases(11085, "grid_frequency", "Grid frequency", Kind.U16, 100, "Hz"),
    *_phases(11088, "eps_voltage", "Backup voltage (EPS)", Kind.U16, 10, "V"),
    *_phases(11091, "eps_current", "Backup current (EPS)", Kind.I16, 10, "A"),
    *_phases(11094, "eps_power", "Backup power (EPS)", Kind.I16, 1, "W"),
    Register(11097, "eps_frequency", "Backup frequency (EPS)", Kind.U16, 100, "Hz"),
    *_phase_powers(11098, "meter1_power", "Meter 1 power", Kind.I16),
    *_phase_powers(11102, "meter2_power", "Meter 2 power", Kind.I16, disabled=True),
    *_phase_powers(11106, "meter3_power", "Meter 3 power", Kind.I16, disabled=True),
    *_phase_powers(11110, "consumption_power", "Consumption power", Kind.I16),
)

# The undocumented 32-bit block: the power values of LIVE again, under the same keys, so that its readings replace
# the 16-bit ones. Verified against them on this unit; not in ALL_REGISTERS.
POWER_32BIT: tuple[Register, ...] = (
    Register(11124, "battery_power", "Battery power", Kind.I32, 1, "W"),
    *_phase_powers(11126, "meter1_power", "Meter 1 power", Kind.I32),
    *_phase_powers(11134, "meter2_power", "Meter 2 power", Kind.I32, disabled=True),
    *_phase_powers(11142, "meter3_power", "Meter 3 power", Kind.I32, disabled=True),
    *_phase_powers(11150, "consumption_power", "Consumption power", Kind.I32),
)


def _energy(address: int, key: str, name: str) -> list[Register]:
    """The lifetime counter (U32) under the plain key and name, and today's (U16), kWh / 10."""
    return [
        Register(address, key, name, Kind.U32, 10, "kWh"),
        Register(address + 2, f"{key}_today", f"{name} today", Kind.U16, 10, "kWh"),
    ]


ENERGY: tuple[Register, ...] = (
    *_energy(14000, "pv_energy", "PV energy"),
    *_energy(14003, "charge_energy", "Battery charge energy"),
    *_energy(14006, "discharge_energy", "Battery discharge energy"),
    *_energy(14009, "grid_export_energy", "Grid export energy"),
    *_energy(14012, "grid_import_energy", "Grid import energy"),
    *_energy(14015, "output_energy", "Output energy"),
    *_energy(14018, "input_energy", "Input energy"),
    *_energy(14021, "consumption_energy", "Consumption energy"),
    *_energy(14024, "eps_energy", "Backup energy (EPS)"),
)

SETTINGS: tuple[Register, ...] = (
    Register(20000, "system_on", "System", Kind.ENUM, enum=ON_OFF, diagnostic=True),
    # The clock's words stay valid in the cloud relay's state as long as their unit: a day, an hour, a minute; seconds 5 s.
    Register(20001, "rtc_year", "Clock year", Kind.U16, diagnostic=True, max_age_s=DAY_S),
    Register(20002, "rtc_month", "Clock month", Kind.U16, diagnostic=True, max_age_s=DAY_S),
    Register(20003, "rtc_day", "Clock day", Kind.U16, diagnostic=True, max_age_s=DAY_S),
    Register(20004, "rtc_hour", "Clock hour", Kind.U16, diagnostic=True, max_age_s=HOUR_S),
    Register(20005, "rtc_minute", "Clock minute", Kind.U16, diagnostic=True, max_age_s=MINUTE_S),
    Register(20006, "rtc_second", "Clock second", Kind.U16, diagnostic=True, max_age_s=CLOCK_SECONDS_MAX_AGE_S),
    Register(20007, "language", "Language", Kind.U16, diagnostic=True),
    Register(20008, "grid_code", "Grid code", Kind.U16, diagnostic=True),
    Register(20009, "meter1_address", "Meter 1 address", Kind.U16, diagnostic=True),
    Register(20010, "meter2_address", "Meter 2 address", Kind.U16, diagnostic=True),
    Register(20011, "meter3_address", "Meter 3 address", Kind.U16, diagnostic=True),
    Register(20012, "modbus_address", "Modbus address", Kind.U16, diagnostic=True),
    Register(20013, "password", "Password", Kind.U16, diagnostic=True, disabled=True),
    Register(20100, "drm0", "DRM0", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(20101, "meter1_enabled", "Meter 1", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(20102, "remote_power_control", "Remote power control", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(20103, "iso_check", "ISO check", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(20104, "gfci_check", "GFCI check", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(20105, "anti_islanding", "Anti-islanding", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(20106, "frequency_protect_restrictive", "Frequency protect restrictive", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(20107, "ground_connect_check", "Ground connect check", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(20108, "phase_unbalance", "Phase unbalance", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(20109, "bypass_relay", "Bypass relay", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(20110, "setting_20110", "Setting 20110", Kind.U16, diagnostic=True, disabled=True),
    Register(20111, "setting_20111", "Setting 20111", Kind.U16, diagnostic=True, disabled=True),
    Register(20112, "setting_20112", "Setting 20112", Kind.U16, diagnostic=True, disabled=True),
    Register(20113, "setting_20113", "Setting 20113", Kind.U16, diagnostic=True, disabled=True),
    Register(21000, "work_mode", "Work mode", Kind.ENUM, enum=WORK_MODES, diagnostic=True),
    Register(21016, "max_charge_current", "Max charge current", Kind.U16, 10, "A", diagnostic=True),
    Register(21017, "max_discharge_current", "Max discharge current", Kind.U16, 10, "A", diagnostic=True),
    Register(21018, "min_soc", "Minimum SoC", Kind.U16, 1, "%", diagnostic=True),
    Register(21019, "min_soc_on_grid", "Minimum SoC on grid", Kind.U16, 1, "%", diagnostic=True),
    Register(21020, "export_limit", "Export limit", Kind.U16, 1, "W", diagnostic=True),
    Register(21021, "power_limit_percent", "Power limit", Kind.I16, 1, "%", diagnostic=True),
)

PROTECTION: tuple[Register, ...] = (
    Register(22000, "connect_enable", "Connect enable", Kind.BITS, bit_names=("soft start", "voltage parameters", "frequency parameters"), diagnostic=True),
    Register(22001, "connect_time", "Connect time", Kind.U16, 1, "s", diagnostic=True),
    Register(22002, "connect_power_gradient", "Connect power gradient", Kind.U16, 10, "%/min", diagnostic=True),
    Register(22003, "reconnect_time", "Reconnect time", Kind.U16, 1, "s", diagnostic=True),
    Register(22004, "reconnect_power_gradient", "Reconnect power gradient", Kind.U16, 10, "%/min", diagnostic=True),
    Register(22005, "connect_voltage_high", "Connect voltage high limit", Kind.U16, 10, "V", diagnostic=True),
    Register(22006, "connect_voltage_low", "Connect voltage low limit", Kind.U16, 10, "V", diagnostic=True),
    Register(22007, "connect_frequency_high", "Connect frequency high limit", Kind.U16, 100, "Hz", diagnostic=True),
    Register(22008, "connect_frequency_low", "Connect frequency low limit", Kind.U16, 100, "Hz", diagnostic=True),
    Register(22100, "voltage_protect_enable", "Voltage protect enable", Kind.BITS, bit_names=("level 1", "level 2", "10 min"), diagnostic=True),
    Register(22101, "voltage_high_1", "Voltage high limit 1", Kind.U16, 10, "V", diagnostic=True),
    # The protect times, here and in the frequency block, and the ride-through times: gain 0.1 in the document, each
    # count 10 ms; the web page shows the protect times so (raw 50, 500 ms).
    Register(22102, "voltage_high_1_time", "Voltage high limit 1 time", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(22103, "voltage_high_2", "Voltage high limit 2", Kind.U16, 10, "V", diagnostic=True),
    Register(22104, "voltage_high_2_time", "Voltage high limit 2 time", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(22105, "voltage_low_1", "Voltage low limit 1", Kind.U16, 10, "V", diagnostic=True),
    Register(22106, "voltage_low_1_time", "Voltage low limit 1 time", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(22107, "voltage_low_2", "Voltage low limit 2", Kind.U16, 10, "V", diagnostic=True),
    Register(22108, "voltage_low_2_time", "Voltage low limit 2 time", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(22109, "voltage_high_10min", "10 min voltage high limit", Kind.U16, 10, "V", diagnostic=True),
    Register(
        22110,
        "voltage_protect_activate",
        "Voltage protect activate",
        Kind.BITS,
        bit_names=("level 1 high", "level 2 high", "level 1 low", "level 2 low", "10 min"),
        diagnostic=True,
    ),
    Register(22200, "frequency_protect_enable", "Frequency protect enable", Kind.BITS, bit_names=("level 1", "level 2"), diagnostic=True),
    Register(22201, "frequency_high_1", "Frequency high limit 1", Kind.U16, 100, "Hz", diagnostic=True),
    Register(22202, "frequency_high_1_time", "Frequency high limit 1 time", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(22203, "frequency_high_2", "Frequency high limit 2", Kind.U16, 100, "Hz", diagnostic=True),
    Register(22204, "frequency_high_2_time", "Frequency high limit 2 time", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(22205, "frequency_low_1", "Frequency low limit 1", Kind.U16, 100, "Hz", diagnostic=True),
    Register(22206, "frequency_low_1_time", "Frequency low limit 1 time", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(22207, "frequency_low_2", "Frequency low limit 2", Kind.U16, 100, "Hz", diagnostic=True),
    Register(22208, "frequency_low_2_time", "Frequency low limit 2 time", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(
        22209,
        "frequency_protect_activate",
        "Frequency protect activate",
        Kind.BITS,
        bit_names=("level 1 high", "level 2 high", "level 1 low", "level 2 low"),
        diagnostic=True,
    ),
    Register(23000, "pf_enable", "P(f) enable", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(23001, "pf_mode", "P(f) mode", Kind.ENUM, enum={0: "f-stop off", 1: "f-stop on"}, diagnostic=True),
    Register(23002, "pf_enter_delay", "P(f) enter delay", Kind.U16, 1, "ms", diagnostic=True),
    Register(23003, "pf_over_start", "P(f) over-frequency start", Kind.U16, 100, "Hz", diagnostic=True),
    Register(23004, "pf_over_droop", "P(f) over-frequency droop", Kind.U16, 100, "%", diagnostic=True),
    Register(23005, "pf_over_recover", "P(f) over-frequency recover", Kind.U16, 100, "Hz", diagnostic=True),
    Register(23006, "pf_under_start", "P(f) under-frequency start", Kind.U16, 100, "Hz", diagnostic=True),
    Register(23007, "pf_under_droop", "P(f) under-frequency droop", Kind.U16, 100, "%", diagnostic=True),
    Register(23008, "pf_under_recover", "P(f) under-frequency recover", Kind.U16, 100, "Hz", diagnostic=True),
    Register(23009, "pf_power_recover_gradient", "P(f) power recover gradient", Kind.U16, 10, "%/min", diagnostic=True),
    Register(23010, "pf_power_recover_delay", "P(f) power recover delay", Kind.U16, 1, "s", diagnostic=True),
    Register(23100, "pu_enable", "P(u) enable", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(23101, "pu_v1", "P(u) V1", Kind.U16, 10, "V", diagnostic=True),
    Register(23102, "pu_v2", "P(u) V2", Kind.U16, 10, "V", diagnostic=True),
    Register(23103, "pu_v3", "P(u) V3", Kind.U16, 10, "V", diagnostic=True),
    Register(23104, "pu_v4", "P(u) V4", Kind.U16, 10, "V", diagnostic=True),
    Register(23105, "pu_p1", "P(u) P1", Kind.U16, 1, "%", diagnostic=True),
    Register(23106, "pu_p4", "P(u) P4", Kind.U16, 1, "%", diagnostic=True),
    Register(23107, "pu_delay", "P(u) delay", Kind.U16, 1, "s", diagnostic=True),
    Register(23108, "pu_decline_gradient", "P(u) power decline gradient", Kind.U16, 1, "%/s", diagnostic=True),
    Register(23200, "reactive_mode", "Reactive power mode", Kind.ENUM, enum=REACTIVE_MODES, diagnostic=True),
    Register(23201, "fixed_pf", "Fixed power factor", Kind.I16, 100, "", diagnostic=True),
    Register(23202, "fixed_q", "Fixed reactive power", Kind.I16, 1, "var", diagnostic=True),
    Register(23203, "cosphi_lock_in_voltage", "Cos phi (P) lock-in voltage", Kind.U16, 10, "V", diagnostic=True),
    Register(23204, "cosphi_lock_out_voltage", "Cos phi (P) lock-out voltage", Kind.U16, 10, "V", diagnostic=True),
    Register(23205, "cosphi_pf1", "Cos phi (P) PF1", Kind.I16, 100, "", diagnostic=True),
    Register(23206, "cosphi_p1", "Cos phi (P) P1", Kind.U16, 1, "%", diagnostic=True),
    Register(23207, "cosphi_pf2", "Cos phi (P) PF2", Kind.I16, 100, "", diagnostic=True),
    Register(23208, "cosphi_p2", "Cos phi (P) P2", Kind.U16, 1, "%", diagnostic=True),
    Register(23209, "cosphi_pf3", "Cos phi (P) PF3", Kind.I16, 100, "", diagnostic=True),
    Register(23210, "cosphi_p3", "Cos phi (P) P3", Kind.U16, 1, "%", diagnostic=True),
    Register(23211, "cosphi_pf4", "Cos phi (P) PF4", Kind.I16, 100, "", diagnostic=True),
    Register(23212, "cosphi_p4", "Cos phi (P) P4", Kind.U16, 1, "%", diagnostic=True),
    Register(23213, "qu_enter_delay", "Q(U) enter delay", Kind.U16, 1, "s", diagnostic=True),
    Register(23214, "qu_lock_in_power", "Q(U) lock-in power", Kind.U16, 1, "%", diagnostic=True),
    Register(23215, "qu_lock_out_power", "Q(U) lock-out power", Kind.U16, 1, "%", diagnostic=True),
    Register(23216, "qu_v1", "Q(U) V1", Kind.U16, 1, "%Un", diagnostic=True),
    Register(23217, "qu_q1", "Q(U) Q1", Kind.I16, 100, "%", diagnostic=True),
    Register(23218, "qu_v2", "Q(U) V2", Kind.U16, 1, "%Un", diagnostic=True),
    Register(23219, "qu_q2", "Q(U) Q2", Kind.I16, 100, "%", diagnostic=True),
    Register(23220, "qu_v3", "Q(U) V3", Kind.U16, 1, "%Un", diagnostic=True),
    Register(23221, "qu_q3", "Q(U) Q3", Kind.I16, 100, "%", diagnostic=True),
    Register(23222, "qu_v4", "Q(U) V4", Kind.U16, 1, "%Un", diagnostic=True),
    Register(23223, "qu_q4", "Q(U) Q4", Kind.I16, 100, "%", diagnostic=True),
    Register(23300, "uvrt_enable", "UVRT enable", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(23301, "ovrt_enable", "OVRT enable", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(23302, "uvrt_v1", "UVRT V1", Kind.U16, 1, "%Un", diagnostic=True),
    Register(23303, "uvrt_t1", "UVRT T1", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(23304, "uvrt_v2", "UVRT V2", Kind.U16, 1, "%Un", diagnostic=True),
    Register(23305, "uvrt_t2", "UVRT T2", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(23306, "uvrt_v3", "UVRT V3", Kind.U16, 1, "%Un", diagnostic=True),
    Register(23307, "uvrt_t3", "UVRT T3", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(23308, "ovrt_v1", "OVRT V1", Kind.U16, 1, "%Un", diagnostic=True),
    Register(23309, "ovrt_t1", "OVRT T1", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(23310, "ovrt_v2", "OVRT V2", Kind.U16, 1, "%Un", diagnostic=True),
    Register(23311, "ovrt_t2", "OVRT T2", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(23312, "ovrt_v3", "OVRT V3", Kind.U16, 1, "%Un", diagnostic=True),
    Register(23313, "ovrt_t3", "OVRT T3", Kind.U16, 1, "ms", step=10, diagnostic=True),
    Register(23314, "zero_current_under_voltage", "Zero current under-voltage threshold", Kind.U16, 1, "%Un", diagnostic=True),
    Register(23315, "zero_current_over_voltage", "Zero current over-voltage threshold", Kind.U16, 1, "%Un", diagnostic=True),
    Register(23400, "rocof_enable", "ROCOF enable", Kind.ENUM, enum=ENABLED, diagnostic=True),
    Register(23401, "rocof_threshold", "ROCOF threshold", Kind.U16, 10, "Hz/s", diagnostic=True),
)

UNDOCUMENTED: tuple[Register, ...] = (
    Register(20114, "register_20114", "Register 20114", Kind.U16, diagnostic=True),
    Register(20130, "register_20130", "Register 20130", Kind.U16, diagnostic=True),
    Register(50028, "register_50028", "Register 50028", Kind.U16, diagnostic=True),
    Register(50029, "register_50029", "Register 50029", Kind.U16, diagnostic=True),
    Register(50030, "register_50030", "Register 50030", Kind.U16, diagnostic=True),
)

# Every register with a key of its own; POWER_32BIT shares the keys of LIVE and is left out.
ALL_REGISTERS: tuple[Register, ...] = IDENTITY + LIVE + ENERGY + SETTINGS + PROTECTION + UNDOCUMENTED

REGISTERS_BY_KEY: dict[str, Register] = {register.key: register for register in ALL_REGISTERS}
