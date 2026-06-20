"""Machine lists and issue categories for the Maintenance Request form."""

from __future__ import annotations

MAINTENANCE_DEPARTMENTS = frozenset({"PDC", "SECONDARY", "FETTLING", "CNC"})


def _opts(items: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"id": i, "title": t} for i, t in items]


def _machines(prefix: str, start: int, end: int) -> list[tuple[str, str]]:
    p = prefix.lower().replace("-", "_")
    return [(f"{p}_{i}", f"{prefix}-{i}") for i in range(start, end + 1)]


def _normalize_dept(dept: str) -> str:
    d = (dept or "").strip().upper()
    if d == "FET":
        return "FETTLING"
    if d in ("SEC", "SECONDARY OPS"):
        return "SECONDARY"
    return d


def _normalize_route(route: str) -> str:
    r = (route or "").strip().upper()
    if r in ("JMD1", "UNIT_I", "UNIT1", "UNIT-1", "UNIT 1"):
        return "JMD1"
    if r in ("JMD2", "UNIT_II", "UNIT2", "UNIT-2", "UNIT 2"):
        return "JMD2"
    return r


UNIT_I_PDC = _opts([
    *[(f"125t_{i}", f"125T-{i}") for i in range(1, 8)],
    ("250t_1", "250T-1"),
    *[(f"350t_{i}", f"350T-{i}") for i in range(1, 5)],
])

UNIT_II_PDC = _opts([
    ("125t_1", "125T-1"),
    ("125t_2", "125T-2"),
    ("250t_1", "250T-1"),
    ("250t_2", "250T-2"),
    *[(f"350t_{i}", f"350T-{i}") for i in range(1, 5)],
    ("500t_1", "500T-1"),
    ("650t_1", "650T-1"),
    ("650t_2", "650T-2"),
    ("800t_1", "800T-1"),
])

UNIT_I_CNC = _opts(
    _machines("CNC", 1, 9) + _machines("VMC", 1, 8)
)

UNIT_II_CNC = _opts(
    _machines("CNC", 1, 6) + _machines("VMC", 1, 15)
)

UNIT_I_FETTLING = _opts([
    *[(f"tm_{i}", f"TM-{i}") for i in (2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15)],
    ("dm_1", "DM-1"),
    ("dm_2", "DM-2"),
    ("mm_1", "MM-1"),
    ("mm_2", "MM-2"),
    ("sb_1", "SB-1"),
    ("sb_2", "SB-2"),
])

UNIT_II_FETTLING = _opts(_machines("TM", 1, 7))

UNIT_I_SECONDARY = _opts(
    _machines("DM", 1, 22)
    + _machines("TM", 1, 14)
    + _machines("GDM", 1, 10)
    + [("gm_1", "GM-1"), ("gm_2", "GM-2"), ("mm_1", "MM-1")]
)

MACHINE_TYPE_OPTIONS: dict[str, list[dict[str, str]]] = {
    "PDC": _opts([("pdc", "PDC")]),
    "SECONDARY": _opts([
        ("drilling", "Drilling"),
        ("gang_drilling", "Gang Drilling"),
        ("tapping", "Tapping"),
        ("milling", "Milling"),
        ("grooving", "Grooving"),
    ]),
    "FETTLING": _opts([
        ("trimming", "Trimming"),
        ("milling", "Milling"),
    ]),
    "CNC": _opts([
        ("cnc", "CNC"),
        ("vmc", "VMC"),
    ]),
}

PDC_ISSUE_CATEGORIES = _opts([
    ("motor", "Motor"),
    ("ladle", "Ladle"),
    ("furnace", "Furnace"),
    ("pump", "Pump"),
    ("extractor", "Extractor"),
    ("sprayer", "Sprayer"),
    ("accumulator", "Accumulator"),
    ("hand_control_box", "Hand Control Box"),
    ("panel_box", "Panel Box"),
    ("lubrication", "Lubrication"),
    ("toggle_link", "Toggle Link"),
    ("shut", "Shut"),
    ("shut_bush", "Shut Bush"),
    ("ladle_encoder", "Ladle Encoder"),
    ("hydraulic_valve", "Hydraulic Valve"),
    ("injection_piston", "Injection Piston"),
    ("ejector_piston", "Ejector Piston"),
    ("die_close_piston", "Die Close Piston"),
])

SECONDARY_ISSUES_BY_MACHINE_TYPE: dict[str, list[dict[str, str]]] = {
    "drilling": _opts([
        ("gearbox", "Gearbox"),
        ("motor_issue", "Motor Issue"),
        ("spindle_problem", "Spindle Problem"),
        ("electrical_issue", "Electrical Issue"),
    ]),
    "gang_drilling": _opts([
        ("sensor_issue", "Sensor Issue"),
        ("auto_mode_issue", "Auto Mode Issue"),
        ("job_clamping_issue", "Job Clamping Issue"),
    ]),
    "tapping": _opts([
        ("spindle_issue", "Spindle Issue"),
        ("contactor_problem", "Contactor Problem"),
        ("motor_problem", "Motor Problem"),
        ("limit_switch", "Limit Switch"),
        ("gearbox", "Gearbox"),
        ("pulley_motor", "Pulley Motor"),
    ]),
    "milling": _opts([
        ("gearbox_problem", "Gearbox Problem"),
        ("bed_movement_issue", "Bed Movement Issue"),
        ("motor_issue", "Motor Issue"),
    ]),
    "grooving": _opts([
        ("hydraulic_power_pack", "Hydraulic Power Pack"),
        ("motor_problem", "Motor Problem"),
        ("pulley", "Pulley"),
        ("electrical_issue", "Electrical Issue"),
        ("spindle_issue", "Spindle Issue"),
    ]),
}

FETTLING_ISSUES_BY_MACHINE_TYPE: dict[str, list[dict[str, str]]] = {
    "trimming": _opts([
        ("power_pack_hydraulic", "Power Pack Hydraulic"),
        ("limit_switch", "Limit Switch"),
        ("hydraulic_cylinder", "Hydraulic Cylinder"),
    ]),
    "milling": _opts([
        ("gearbox_problem", "Gearbox Problem"),
        ("bed_movement_issue", "Bed Movement Issue"),
        ("motor_issue", "Motor Issue"),
    ]),
}

CNC_ISSUE_CATEGORIES = _opts([
    ("turret_not_clamped", "Turret not clamped"),
    ("turret_index_timeout", "Turret index timeout"),
    ("chuck_not_unclamped", "Chuck not unclamped"),
    ("chuck_not_clamped", "Chuck not clamped"),
    ("chuck_clamping_pressure_low", "Chuck clamping pressure low"),
    ("hydraulic_pressure_down", "Hydraulic pressure down"),
    ("insufficient_lubrication_oil", "Insufficient lubrication oil"),
    ("lubrication_pressure_still_down", "Lubrication pressure still down"),
    ("illegal_current_loop", "Illegal current loop"),
    ("illegal_gear_ratio_parameter", "Illegal gear ratio parameter"),
    ("apc_alarm", "APC alarm"),
    ("spindle_alarm", "Spindle alarm"),
    ("x_axis_vrdy_off", "X-Axis VRDY off"),
    ("z_axis_vrdy_off", "Z-Axis VRDY off"),
    ("axis_turret_inch_on", "Axis turret inch on"),
    ("axis_excess_error", "Axis excess error"),
    ("inverter_ipm_alarm", "Inverter IPM alarm"),
    ("emergency_stop_alarm", "Emergency stop alarm"),
    ("servo_alarm", "Servo alarm"),
    ("hydraulic_pump_overheat", "Hydraulic pump overheat"),
    ("spindle_motor_overheat", "Spindle motor overheat"),
    ("drive_overload", "Drive overload"),
    ("safety_interlock", "Safety interlock"),
    ("overcurrent_power_circuit", "Overcurrent power circuit"),
    ("chuck_active_overtime", "Chuck active overtime"),
    ("tool_change_cycle_interrupted", "Tool change cycle interrupted"),
    ("ac_control_cabinet_temp_high", "AC control cabinet temp high"),
    ("cooling_fan_failure", "Cooling fan failure"),
    ("tripped_circuit_breaker", "Tripped circuit breaker"),
])

MACHINE_LABELS: dict[str, str] = {
    opt["id"]: opt["title"]
    for group in (
        UNIT_I_PDC,
        UNIT_II_PDC,
        UNIT_I_CNC,
        UNIT_II_CNC,
        UNIT_I_FETTLING,
        UNIT_II_FETTLING,
        UNIT_I_SECONDARY,
    )
    for opt in group
}

ISSUE_LABELS: dict[str, str] = {
    opt["id"]: opt["title"]
    for group in (
        PDC_ISSUE_CATEGORIES,
        CNC_ISSUE_CATEGORIES,
        *SECONDARY_ISSUES_BY_MACHINE_TYPE.values(),
        *FETTLING_ISSUES_BY_MACHINE_TYPE.values(),
    )
    for opt in group
}

MACHINE_TYPE_LABELS: dict[str, str] = {
    opt["id"]: opt["title"]
    for opts in MACHINE_TYPE_OPTIONS.values()
    for opt in opts
}


def _filter_by_prefix(machines: list[dict[str, str]], prefix: str) -> list[dict[str, str]]:
    p = prefix.lower()
    return [m for m in machines if m["id"].startswith(p)]


def _all_machines(dept: str, route: str) -> list[dict[str, str]]:
    dept = _normalize_dept(dept)
    route = _normalize_route(route)
    if dept == "PDC":
        return UNIT_I_PDC if route == "JMD1" else UNIT_II_PDC
    if dept == "CNC":
        return UNIT_I_CNC if route == "JMD1" else UNIT_II_CNC
    if dept == "FETTLING":
        return UNIT_I_FETTLING if route == "JMD1" else UNIT_II_FETTLING
    if dept == "SECONDARY":
        return UNIT_I_SECONDARY if route == "JMD1" else []
    return []


def machine_type_options(dept: str) -> list[dict[str, str]]:
    dept = _normalize_dept(dept)
    return list(MACHINE_TYPE_OPTIONS.get(dept, []))


def machine_no_options(dept: str, route: str, machine_type: str) -> list[dict[str, str]]:
    dept = _normalize_dept(dept)
    route = _normalize_route(route)
    mtype = (machine_type or "").strip().lower()
    all_m = _all_machines(dept, route)

    if dept == "PDC":
        return all_m
    if dept == "CNC":
        if mtype == "cnc":
            return _filter_by_prefix(all_m, "cnc_")
        if mtype == "vmc":
            return _filter_by_prefix(all_m, "vmc_")
        return []
    if dept == "FETTLING":
        if mtype == "trimming":
            return _filter_by_prefix(all_m, "tm_")
        if mtype == "milling":
            return _filter_by_prefix(all_m, "mm_")
        return []
    if dept == "SECONDARY":
        prefix_map = {
            "drilling": "dm_",
            "gang_drilling": "gdm_",
            "tapping": "tm_",
            "milling": "mm_",
            "grooving": "gm_",
        }
        prefix = prefix_map.get(mtype, "")
        return _filter_by_prefix(all_m, prefix) if prefix else []
    return []


def issue_category_options(dept: str, machine_type: str) -> list[dict[str, str]]:
    dept = _normalize_dept(dept)
    mtype = (machine_type or "").strip().lower()

    if dept == "PDC":
        return PDC_ISSUE_CATEGORIES
    if dept == "CNC":
        return CNC_ISSUE_CATEGORIES
    if dept == "SECONDARY":
        return list(SECONDARY_ISSUES_BY_MACHINE_TYPE.get(mtype, []))
    if dept == "FETTLING":
        return list(FETTLING_ISSUES_BY_MACHINE_TYPE.get(mtype, []))
    return []


def default_machine_type(dept: str) -> str:
    dept = _normalize_dept(dept)
    if dept == "PDC":
        return "pdc"
    return ""


def is_supported_department(dept: str) -> bool:
    return _normalize_dept(dept) in MAINTENANCE_DEPARTMENTS
