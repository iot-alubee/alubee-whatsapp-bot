"""Issue catalogs and department rules for Tool Breakdown requests."""

from __future__ import annotations

from maintenance_data import machine_no_options

TOOL_BREAKDOWN_DEPARTMENTS = frozenset({"PDC", "SECONDARY", "FETTLING"})


def _opts(items: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"id": i, "title": t} for i, t in items]


def _normalize_dept(dept: str) -> str:
    d = (dept or "").strip().upper()
    if d == "FET":
        return "FETTLING"
    if d in ("SEC", "SECONDARY OPS"):
        return "SECONDARY"
    return d


FETTLING_TOOL_ISSUES = _opts([
    ("tapping_hole_pin_broken", "Tapping Hole Pin – Broken"),
    ("profile_pin_broken", "Profile Pin – Broken"),
    ("pin_damage_6_10", "Pin Damage – 6.10"),
    ("pin_damage_5_50", "Pin Damage – 5.50"),
    ("pin_damage_4_30", "Pin Damage – 4.30"),
    ("pin_damage_5_00", "Pin Damage – 5.00"),
    ("pin_damage_6_50", "Pin Damage – 6.50"),
    ("pin_damage_8_30", "Pin Damage – 8.30"),
    ("pin_damage_5_30", "Pin Damage – 5.30"),
    ("pin_damage_5_40", "Pin Damage – 5.40"),
    ("spring_broken", "Spring – Broken"),
    ("ejector_pin_broken", "Ejector Pin – Broken"),
    ("piston_rod_bent", "Piston Rod – Bent"),
    ("limit_switch_rod_bent", "Limit Switch Rod – Bent"),
    ("overall_bend", "Overall Bend"),
    ("trimming_die_jam", "Trimming Die – Jam"),
    ("spring_bush_issue", "Spring Bush – Issue"),
])

SECONDARY_TOOL_ISSUES = _opts([
    ("fixture_pin_issue", "Fixture Pin – Issue"),
    ("clamp_issue", "Clamp – Issue"),
    ("pin_tightness_issue", "Pin Tightness – Issue"),
    ("cylinder_issue", "Cylinder – Issue"),
    ("tap_check_issue", "Tap Check – Issue"),
    ("drill_adapter_issue", "Drill Adapter – Issue"),
    ("insert_screw_broken", "Insert Screw – Broken"),
    ("tapping_machine_adapter_replaced", "Tapping Machine Adapter – Replaced"),
    ("machine_stopper_adjuster_issue", "Machine Stopper Adjuster – Issue"),
])

PDC_TOOL_ISSUES = _opts([
    ("injection_piston_rod", "Injection Piston Rod"),
    ("die_close_cylinder_piston", "Die Close Cylinder Piston"),
    ("ejector_cylinder_piston", "Ejector Cylinder Piston"),
    ("toggle_shaft", "Toggle Shaft"),
    ("toggle_bush", "Toggle Bush"),
    ("core_pin", "Core Pin"),
    ("ejector_pin", "Ejector Pin"),
    ("plunger_rod", "Plunger Rod"),
    ("plunger_block", "Plunger Block"),
    ("finger_cam", "Finger Cam"),
    ("die_sleeve", "Die Sleeve"),
    ("machine_sleeve", "Machine Sleeve"),
    ("side_core_holder", "Side Core Holder"),
    ("insert_profile", "Insert Profile"),
    ("ejector_rod", "Ejector Rod"),
    ("die_polishing", "Die Polishing"),
    ("stock_correction", "Stock Correction"),
    ("toggle_link", "Toggle Link"),
    ("limit_switch_rod", "Limit Switch Rod"),
    ("toggle_key", "Toggle Key"),
    ("side_core", "Side Core"),
])

ISSUE_OPTIONS_BY_DEPT: dict[str, list[dict[str, str]]] = {
    "FETTLING": FETTLING_TOOL_ISSUES,
    "SECONDARY": SECONDARY_TOOL_ISSUES,
    "PDC": PDC_TOOL_ISSUES,
}

ISSUE_LABELS: dict[str, str] = {
    opt["id"]: opt["title"]
    for group in ISSUE_OPTIONS_BY_DEPT.values()
    for opt in group
}


def is_supported_department(dept: str) -> bool:
    return _normalize_dept(dept) in TOOL_BREAKDOWN_DEPARTMENTS


def issue_options(dept: str) -> list[dict[str, str]]:
    return list(ISSUE_OPTIONS_BY_DEPT.get(_normalize_dept(dept), []))


def normalize_tool_name(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


__all__ = [
    "ISSUE_LABELS",
    "TOOL_BREAKDOWN_DEPARTMENTS",
    "issue_options",
    "is_supported_department",
    "machine_no_options",
    "normalize_tool_name",
]
