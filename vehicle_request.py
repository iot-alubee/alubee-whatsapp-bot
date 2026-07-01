"""Vehicle request flow — WhatsApp Form submit, logistics manager assign/cancel.

Access vs assignment (do not conflate):
  - is_logistics_requester  → may open the Vehicle Request form (any department)
  - INTERNAL_VEHICLE_ASSIGNEES → staff listed when manager assigns Internal vehicles
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]

from bot_shared import get_user_record, wa_from_10
from interakt_api import (
    ensure_customer,
    send_list_menu,
    send_reply_buttons,
    send_template,
    send_vehicle_request_flow_form,
    wa_id_to_phone,
)

logger = logging.getLogger(__name__)

VEHICLE_REQUEST_OPEN_STATUSES = frozenset({
    "PENDING", "ASSIGNED", "STARTED", "IN_PROGRESS",
})
SESSION_WAITING_VEHICLE_ASSIGN = "WAITING_VEHICLE_ASSIGN_PICK"
SESSION_WAITING_VEHICLE_ASSIGN_TYPE = "WAITING_VEHICLE_ASSIGN_TYPE"
SESSION_WAITING_VEHICLE_FLEET_PICK = "WAITING_VEHICLE_FLEET_PICK"
SESSION_WAITING_VEHICLE_MANAGER_NOTIFY = "WAITING_VEHICLE_MANAGER_NOTIFY"
SESSION_WAITING_VEHICLE_MANAGE_ACTION = "WAITING_VEHICLE_MANAGE_ACTION"
SESSION_WAITING_VEHICLE_REASSIGN = "WAITING_VEHICLE_REASSIGN_PICK"
SESSION_WAITING_VEHICLE_REASSIGN_FLEET = "WAITING_VEHICLE_REASSIGN_FLEET_PICK"

REQUEST_TYPE_LABELS = {
    "delivery": "Delivery",
    "pickup": "Pickup",
    "delivery_and_pickup": "Delivery and Pickup",
}

DESTINATION_CATEGORY_LABELS = {
    "supplier": "Supplier",
    "sub_contractor": "Sub Contractor",
    "customer": "Customer",
    "purchase": "Purchase",
    "transport_office": "Transport Office",
    "other_unit": "Other Unit",
}

DESTINATION_LABELS = {
    "unit_1_to_unit_2": "Unit 1 to Unit 2",
    "unit_2_to_unit_1": "Unit 2 to Unit 1",
    "vs_industries": "VS Industries",
    "vinayagam": "Vinayagam",
    "yogesh": "Yogesh",
    "jayashakthi": "Jayashakthi",
    "rajeshwari": "Rajeshwari",
    "kamaraj_nagar": "Kamaraj Nagar",
    "tvs": "TVS",
    "neocol": "Neocol",
    "v_tech": "V-Tech",
    "arasanatti": "Arasanatti",
    "guest_line": "Guest Line",
    "alloy_tech": "Alloy Tech",
    "amara_raja": "Amara Raja",
    "lakshmi_steels": "Lakshmi Steels",
    "chellam_transport": "Chellam Transport",
    "seenu_transport": "Seenu Transport",
    "madhumitha": "Madhumitha",
    "rashi": "Rashi",
    "rajeshwari_layout": "Rajeshwari Layout",
    "local_hosur": "Hosur Local",
    "ayyappa_gas_agency": "Ayyappa Gas Agency",
    "md_office": "MD Office",
    "bangalore": "Bangalore",
    "others": "Others",
    "seg_hosur": "SEG Hosur",
    "seg_ka": "SEG KA",
    "valli_industrial": "Valli Industrial",
    # Legacy ids (older requests / prior lists)
    "ayyappa_gas_shop": "Ayyappa Gas Shop",
    "ayyappa_gas_godown": "Ayyappa Gas Godown",
    "local_shipcot_area": "Local Shipcot Area",
    "bagalur_road": "Bagalur Road",
    "seg_mould_inspection": "SEG Mould Inspection",
    "kamal": "Kamal",
    "kamaraj_nagar_supplier": "Kamaraj Nagar Supplier",
    "unit_i": "Unit I",
    "unit_ii": "Unit II",
}

DESTINATION_DISTANCE_KM: dict[str, int] = {
    "unit_1_to_unit_2": 3,
    "unit_2_to_unit_1": 3,
    "neocol": 6,
    "v_tech": 4,
    "chellam_transport": 24,
    "seenu_transport": 24,
    "ayyappa_gas_agency": 6,
    "ayyappa_gas_shop": 6,
    "ayyappa_gas_godown": 15,
    "local_shipcot_area": 3,
    "local_hosur": 3,
    "alloy_tech": 17,
    "arasanatti": 5,
    "bagalur_road": 10,
    "seg_hosur": 50,
    "seg_mould_inspection": 50,
    "rajeshwari_layout": 5,
    "rajeshwari": 5,
    "kamal": 2,
    "lakshmi_steels": 6,
    "kamaraj_nagar": 4,
    "kamaraj_nagar_supplier": 4,
    "tvs": 20,
    "amara_raja": 420,
    "unit_i": 3,
    "unit_ii": 3,
}

VEHICLE_TYPE_LABELS = {
    "in_house": "Internal",
    "external_hire": "External",
}

HIRE_VEHICLE_TYPE_LABELS = {
    "dost": "Dost",
    "eicher": "Eicher",
    "auto": "Auto",
}

LOAD_SIZE_LABELS = {
    "full_load": "Full Load",
    "half_load": "Half Load",
    "quarter_load": "Quarter Load",
    "single_item": "Single Item",
    "empty_vehicle": "Empty Vehicle",
}

FROM_UNIT_LABELS = {
    "unit_i": "Unit I",
    "unit_ii": "Unit II",
}

_FROM_UNIT_ALIASES: dict[str, str] = {
    "unit_i": "unit_i",
    "unit_1": "unit_i",
    "unit_ii": "unit_ii",
    "unit_2": "unit_ii",
}

_VEHICLE_ALL_TIME_SLOTS: tuple[str, ...] = (
    "08:30", "09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
    "12:00", "12:30", "13:00", "13:30", "14:00", "14:30", "15:00",
    "15:30", "16:00", "16:30", "17:00", "17:30", "18:00", "18:30",
    "19:00", "19:30", "20:00",
)

_VEHICLE_REQUIRED_TIME_SLOTS: frozenset[str] = frozenset(_VEHICLE_ALL_TIME_SLOTS)


def _time_slot_minutes(hhmm: str) -> int:
    hour, minute = map(int, hhmm.split(":"))
    return hour * 60 + minute


def _normalize_required_time_hhmm(raw: str) -> str:
    time_norm = (raw or "").strip().replace(".", ":").upper()
    if ":" in time_norm and time_norm in _VEHICLE_REQUIRED_TIME_SLOTS:
        return time_norm
    norm = _normalize_id(time_norm).replace("_", ":")
    if norm in _VEHICLE_REQUIRED_TIME_SLOTS:
        return norm
    return ""


def _allowed_required_time_slots(now: datetime | None = None) -> frozenset[str]:
    """Slots strictly after current IST time, through 8:00 PM."""
    current = now or _ist_now()
    now_minutes = current.hour * 60 + current.minute
    return frozenset(
        slot
        for slot in _VEHICLE_ALL_TIME_SLOTS
        if _time_slot_minutes(slot) > now_minutes
    )

EXTERNAL_VENDORS: list[tuple[str, str]] = [
    ("annai_transport", "Annai Transport"),
    ("challa_transport", "Challa Transport"),
    ("sridhar_transport", "Sridhar Transport"),
    ("chella_transport", "Chella Transport"),
]

INTERNAL_VEHICLE_ASSIGNEES: list[tuple[str, str]] = [
    ("adc239", "Arun Selvam"),
    ("adc324", "Pandiarajan"),
    ("cl004", "Ajay"),
]

INTERNAL_FLEET_VEHICLES: list[tuple[str, str]] = [
    ("dost_3271", "Dost-3271"),
    ("dost_2568", "Dost-2568"),
    ("santro_2004", "Santro-2004"),
    ("santa_fe_1666", "Santa FE-1666"),
]

_EXTERNAL_VENDOR_LABELS: dict[str, str] = dict(EXTERNAL_VENDORS)
_EXTERNAL_VENDOR_CODES = frozenset(_EXTERNAL_VENDOR_LABELS)

_MANUAL_CATEGORIES = frozenset({"purchase", "transport_office"})
_DROPDOWN_CATEGORIES = frozenset({"supplier", "sub_contractor", "customer", "other_unit"})

_OTHER_UNIT_DESTINATION_BY_FROM: dict[str, str] = {
    "unit_i": "unit_ii",
    "unit_ii": "unit_i",
}

_SUPPLIER_DESTINATIONS: frozenset[str] = frozenset({
    "unit_1_to_unit_2",
    "unit_2_to_unit_1",
    "vs_industries",
    "vinayagam",
    "yogesh",
    "jayashakthi",
    "rajeshwari",
    "kamaraj_nagar",
    "neocol",
    "v_tech",
    "arasanatti",
    "guest_line",
    "alloy_tech",
    "lakshmi_steels",
    "chellam_transport",
    "seenu_transport",
    "madhumitha",
    "rashi",
    "rajeshwari_layout",
    "kamal",
    "local_hosur",
    "ayyappa_gas_agency",
    "md_office",
    "bangalore",
    "others",
    "seg_hosur",
    "seg_ka",
    "valli_industrial",
})

VALID_DESTINATIONS_BY_CATEGORY: dict[str, frozenset[str]] = {
    "supplier": _SUPPLIER_DESTINATIONS,
    "customer": frozenset({"tvs", "amara_raja"}),
    "sub_contractor": frozenset({
        "rajeshwari_layout", "kamal", "lakshmi_steels", "kamaraj_nagar_supplier",
    }),
}


@dataclass
class VehicleRequestDeps:
    db: object
    send_to: Callable[[str, str], None]
    session_merge: Callable[..., None]
    session_ref: Callable[[str], object]
    utcnow: Callable
    clear_session: Callable[[str], None]
    go_main_menu: Callable[[str], None]
    same_whatsapp: Callable[[str, str], bool]
    has_active_whatsapp_session: Callable[[str], bool]


def can_raise_vehicle_request(
    user_data: dict | None,
    *,
    sender: str = "",
    same_whatsapp: Callable[[str, str], bool] | None = None,
) -> bool:
    """Logistics *flag* — not department LOGISTICS. Manager cannot raise requests."""
    if sender and same_whatsapp and is_logistics_manager(sender, same_whatsapp):
        return False
    return bool(user_data and user_data.get("is_logistics_requester"))


def show_vehicle_menu_for_user(
    user_data: dict | None,
    wa_id: str,
    same_whatsapp: Callable[[str, str], bool],
) -> bool:
    """Show Vehicle menu row for logistics requesters or the logistics manager."""
    if is_logistics_manager(wa_id, same_whatsapp):
        return True
    return can_raise_vehicle_request(user_data)


def is_vehicle_manage_action_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_VEHICLE_MANAGE_ACTION


def is_vehicle_reassign_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_VEHICLE_REASSIGN


def _ist_now() -> datetime:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _ist_day_bounds(day) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=_ist_now().tzinfo)
    end = start + timedelta(days=1)
    return start, end


def _request_on_ist_day(rd: dict, day) -> bool:
    ts = rd.get("requested_datetime")
    if ts is None:
        return False
    if hasattr(ts, "timestamp"):
        dt = ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(_ist_now().tzinfo)
    else:
        return False
    return local.date() == day


def _trip_started(rd: dict) -> bool:
    return _request_status(rd) == "STARTED"


_ROMAN_NUMERAL_CHARS = frozenset("IVXLCDM")


def _sentence_case_word(word: str) -> str:
    if not word:
        return word
    up = word.upper()
    if len(up) <= 8 and all(c in _ROMAN_NUMERAL_CHARS for c in up):
        return up
    return word[:1].upper() + word[1:].lower()


def _sentence_case_name(value: str) -> str:
    s = (value or "").strip()
    if not s or s == "—":
        return s or "—"
    normalized = re.sub(r"[_\s]+", " ", s)
    parts = [p for p in normalized.split(" ") if p]
    if not parts:
        return "—"
    return " ".join(_sentence_case_word(p) for p in parts)


def _manage_row_line(rd: dict) -> str:
    requester = _sentence_case_name(rd.get("employee_name") or "—")
    destination = (rd.get("destination_label") or "—").strip()
    assignee = _sentence_case_name(rd.get("assigned_to") or "—")
    if _request_status(rd) == "PENDING":
        assignee = "—"
    time_val = (rd.get("required_at") or "—").strip()
    return f"{requester} - {destination} - {assignee} - {time_val}"


def _manage_row_title(rd: dict) -> str:
    return _manage_row_line(rd)[:72]


def _manage_list_row_fields(request_id: str, rd: dict) -> dict[str, str]:
    """WhatsApp list row: title + optional continuation (no duplicated text)."""
    parts = _manage_row_line(rd).split(" - ")
    title_parts: list[str] = []
    for part in parts:
        candidate = " - ".join(title_parts + [part])
        if len(candidate) <= 24:
            title_parts.append(part)
        else:
            break
    title = " - ".join(title_parts) if title_parts else _manage_row_line(rd)[:24]
    remainder = parts[len(title_parts):]
    row: dict[str, str] = {
        "id": _manage_list_row_id(request_id),
        "title": title[:24],
    }
    if remainder:
        row["description"] = " - ".join(remainder)[:72]
    return row


def _manage_list_row_id(request_id: str) -> str:
    return f"VMANAGE_{request_id}"[:256]


def _logistics_department_name() -> str:
    return (
        os.getenv("VEHICLE_INTERNAL_ASSIGN_DEPARTMENT")
        or os.getenv("LOGISTICS_DEPARTMENT_NAME")
        or "LOGISTICS"
    ).strip().upper()


def _logistics_department_staff(db: object) -> list[tuple[str, str]]:
    """Internal vehicle assignees (fixed list; WA resolved from Firestore by employee_id)."""
    del db  # list is static; WA lookup uses employee_id at assign/notify time
    return list(INTERNAL_VEHICLE_ASSIGNEES)


def _staff_wa_for_assignee_code(
    db: object, assignee_code: str
) -> tuple[str, str] | None:
    """Resolve internal assignee WhatsApp id + name from employee_id code."""
    code = _normalize_id(assignee_code)
    if not code:
        return None
    try:
        for snap in db.collection("users").stream():
            ud = snap.to_dict() or {}
            emp_id = _normalize_id(ud.get("employee_id") or "")
            if emp_id == code:
                name = (ud.get("name") or assignee_code).strip()
                return snap.id, name
    except Exception:
        logger.exception(
            "staff wa lookup failed code=%s", assignee_code
        )
        return None
    return None


def is_vehicle_assign_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_VEHICLE_ASSIGN


def is_vehicle_assign_type_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_VEHICLE_ASSIGN_TYPE


def is_vehicle_fleet_pick_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_VEHICLE_FLEET_PICK


def is_vehicle_reassign_fleet_pick_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_VEHICLE_REASSIGN_FLEET


def _flow_template_env_name() -> str:
    return (
        os.getenv("VEHICLE_REQUEST_FLOW_TEMPLATE_NAME")
        or os.getenv("LOGISTICS_FLOW_TEMPLATE_NAME")
        or ""
    ).strip()


def vehicle_request_flow_template_name() -> str:
    return _flow_template_env_name()


def vehicle_request_flow_enabled() -> bool:
    return bool(_flow_template_env_name())


def _approval_template_name() -> str:
    return (
        os.getenv("VEHICLE_REQUEST_APPROVAL_TEMPLATE_NAME")
        or os.getenv("LOGISTICS_MANAGER_APPROVAL_TEMPLATE_NAME")
        or ""
    ).strip()


def _approval_template_language() -> str:
    return (
        os.getenv("VEHICLE_REQUEST_APPROVAL_TEMPLATE_LANGUAGE_CODE")
        or os.getenv("LOGISTICS_MANAGER_APPROVAL_TEMPLATE_LANGUAGE_CODE")
        or "en"
    ).strip()


def _approval_template_body_fields() -> list[str]:
    raw = (
        os.getenv("VEHICLE_REQUEST_APPROVAL_TEMPLATE_BODY_FIELDS")
        or os.getenv("LOGISTICS_MANAGER_APPROVAL_TEMPLATE_BODY_FIELDS")
        or (
            "requester,department,from,request_type,category,destination,vehicle_type,"
            "hire_vehicle_type,load_capacity,distance,required_at"
        )
    ).strip()
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _assignee_notify_template_name() -> str:
    return (
        os.getenv("VEHICLE_ASSIGNEE_NOTIFY_TEMPLATE_NAME")
        or os.getenv("VEHICLE_INTERNAL_ASSIGNEE_TEMPLATE_NAME")
        or "vehicle_assignee_v02"
    ).strip()


def _assignee_notify_template_language() -> str:
    return (
        os.getenv("VEHICLE_ASSIGNEE_NOTIFY_TEMPLATE_LANGUAGE_CODE")
        or os.getenv("VEHICLE_INTERNAL_ASSIGNEE_TEMPLATE_LANGUAGE_CODE")
        or "en"
    ).strip()


def _assignee_notify_template_body_fields() -> list[str]:
    raw = (
        os.getenv("VEHICLE_ASSIGNEE_NOTIFY_TEMPLATE_BODY_FIELDS")
        or os.getenv("VEHICLE_INTERNAL_ASSIGNEE_TEMPLATE_BODY_FIELDS")
        or "assignee_name,requester,from,request_type,category,destination,vehicle,time"
    ).strip()
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _assignee_time_label(rd: dict) -> str:
    raw = rd.get("required_at")
    if raw is None or raw == "":
        return "—"
    if isinstance(raw, str):
        return raw.strip() or "—"
    return str(raw).strip() or "—"


def _assignee_notify_template_values(rd: dict, assignee_name: str) -> dict[str, str]:
    vehicle = (
        (rd.get("fleet_vehicle_label") or "").strip()
        or (rd.get("external_vehicle_number") or "").strip()
        or "—"
    )
    return {
        "assignee_name": _sentence_case_name(assignee_name or "—"),
        "requester": _sentence_case_name(rd.get("employee_name") or "—"),
        "from": rd.get("from_unit_label") or "—",
        "request_type": rd.get("request_type_label") or "—",
        "category": rd.get("destination_category_label") or "—",
        "destination": rd.get("destination_label") or "—",
        "vehicle": vehicle,
        "time": _assignee_time_label(rd),
    }


def _assignee_notify_template_body_values(rd: dict, assignee_name: str) -> list[str]:
    values = _assignee_notify_template_values(rd, assignee_name)
    fields = _assignee_notify_template_body_fields()
    if len(fields) != 8:
        logger.warning(
            "VEHICLE_ASSIGNEE_NOTIFY_TEMPLATE_BODY_FIELDS should list exactly 8 "
            "fields for vehicle_assignee_message; got %s",
            len(fields),
        )
    return [values.get(key, "—")[:1024] for key in fields]


def _notify_internal_assignee(
    deps: VehicleRequestDeps,
    rd: dict,
    *,
    assignee_code: str,
    assignee_label: str,
    request_id: str,
) -> None:
    """Notify internal assignee via WhatsApp template only (vehicle_assignee_v02)."""
    if _normalize_vehicle_type(rd.get("vehicle_type") or "") != "in_house":
        return

    found = _staff_wa_for_assignee_code(deps.db, assignee_code)
    if not found:
        logger.warning(
            "vehicle assignee notify skipped — no user for code=%s label=%s",
            assignee_code,
            assignee_label,
        )
        return

    assignee_wa, assignee_name = found
    display_name = _sentence_case_name(assignee_label or assignee_name)
    template_name = _assignee_notify_template_name()
    if not template_name:
        logger.error(
            "vehicle assignee notify skipped — VEHICLE_ASSIGNEE_NOTIFY_TEMPLATE_NAME not set request_id=%s",
            request_id,
        )
        return

    phone = wa_id_to_phone(assignee_wa)
    rid = (request_id or "").strip()
    body_values = _assignee_notify_template_body_values(rd, display_name)

    try:
        ensure_customer(phone, name=display_name)
        send_template(
            phone,
            template_name,
            language_code=_assignee_notify_template_language(),
            body_values=body_values,
            callback_data=rid,
            ensure_contact=False,
        )
        logger.info(
            "vehicle assignee template sent assignee=%s request_id=%s template=%s fields=%s",
            assignee_wa,
            request_id,
            template_name,
            len(body_values),
        )
    except Exception:
        logger.exception(
            "vehicle assignee template failed assignee=%s request_id=%s template=%s",
            assignee_wa,
            request_id,
            template_name,
        )


def _flow_pick(data: dict, *keys: str) -> str:
    for key in keys:
        val = data.get(key)
        if isinstance(val, dict):
            val = val.get("id") or val.get("title")
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _normalize_id(raw: str) -> str:
    return (raw or "").strip().lower().replace(" ", "_").replace("-", "_")


_VEHICLE_TYPE_ALIASES: dict[str, str] = {
    "in_house": "in_house",
    "in_house_vehicle": "in_house",
    "internal": "in_house",
    "company_vehicle": "in_house",
    "external_hire": "external_hire",
    "external": "external_hire",
    "external_vehicle": "external_hire",
    "hire": "external_hire",
}


def _normalize_vehicle_type(raw: str) -> str:
    norm = _normalize_id(raw)
    return _VEHICLE_TYPE_ALIASES.get(norm, norm)


def _normalize_from_unit(raw: str) -> str:
    norm = _normalize_id(raw)
    return _FROM_UNIT_ALIASES.get(norm, norm)


def _format_time_12h(hhmm: str) -> str:
    raw = (hhmm or "").strip()
    if not raw:
        return "—"
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p"):
        try:
            parsed = datetime.strptime(raw, fmt)
            hour = parsed.hour % 12 or 12
            ampm = "AM" if parsed.hour < 12 else "PM"
            return f"{hour}:{parsed.minute:02d} {ampm}"
        except ValueError:
            continue
    return raw


def _build_required_at(data: dict) -> str:
    legacy = _flow_pick(data, "required_at")
    if legacy:
        return legacy

    req_time_raw = _flow_pick(data, "required_time")
    if not req_time_raw:
        return "—"

    time_norm = _normalize_required_time_hhmm(req_time_raw)
    if time_norm in _VEHICLE_REQUIRED_TIME_SLOTS:
        return _format_time_12h(time_norm)
    return _format_time_12h(req_time_raw)


def _destination_display(category: str, destination: str, location: str) -> str:
    if category in _MANUAL_CATEGORIES:
        return (location or "—").strip()
    dest_id = _normalize_id(destination)
    return DESTINATION_LABELS.get(dest_id, destination or "—")


def _estimated_distance_km(category: str, destination: str) -> int | None:
    if category in _MANUAL_CATEGORIES:
        return None
    dest_id = _normalize_id(destination)
    return DESTINATION_DISTANCE_KM.get(dest_id)


def _logistics_manager_wa() -> str:
    raw = (
        os.getenv("LOGISTICS_MANAGER_WHATSAPP_NUMBER")
        or os.getenv("VEHICLE_REQUEST_NOTIFY_WHATSAPP_NUMBER")
        or os.getenv("LOGISTICS_WHATSAPP_NUMBER")
        or ""
    ).strip()
    if not raw:
        return ""
    return wa_from_10(wa_id_to_phone(raw)[-10:])


def is_logistics_manager(sender: str, same_whatsapp: Callable[[str, str], bool]) -> bool:
    mgr = _logistics_manager_wa()
    return bool(mgr and same_whatsapp(sender, mgr))


def find_open_vehicle_request(employee: str, db: object) -> tuple[str, dict] | None:
    from bot_shared import query_requests_for_employee

    for req_type in ("VEHICLE_REQUEST", "LOGISTICS"):
        for snap in query_requests_for_employee(db, req_type, employee):
            rd = snap.to_dict() or {}
            status = (
                rd.get("vehicle_request_status")
                or rd.get("logistics_status")
                or "PENDING"
            ).strip().upper()
            if status in VEHICLE_REQUEST_OPEN_STATUSES:
                return snap.id, rd
    return None


def _load_request(db: object, request_id: str) -> tuple[object, dict] | None:
    rid = (request_id or "").strip()
    if not rid:
        return None
    ref = db.collection("requests").document(rid)
    snap = ref.get()
    if not snap.exists:
        return None
    rd = snap.to_dict() or {}
    if (rd.get("type") or "").strip().upper() not in ("VEHICLE_REQUEST", "LOGISTICS"):
        return None
    return ref, rd


def _request_status(rd: dict) -> str:
    return (
        rd.get("vehicle_request_status")
        or rd.get("logistics_status")
        or "PENDING"
    ).strip().upper()


def parse_flow_response(response_json: dict | str | None) -> dict | None:
    if response_json is None:
        return None
    if isinstance(response_json, str):
        try:
            data = json.loads(response_json)
        except json.JSONDecodeError:
            return None
    elif isinstance(response_json, dict):
        data = response_json
    else:
        return None
    if not isinstance(data, dict) or not data:
        return None

    request_type = _normalize_id(_flow_pick(data, "request_type"))
    if request_type not in REQUEST_TYPE_LABELS:
        return None

    from_unit = _normalize_from_unit(_flow_pick(data, "from_unit"))
    if from_unit and from_unit not in FROM_UNIT_LABELS:
        logger.warning(
            "vehicle request parse failed: from_unit=%r keys=%s",
            from_unit,
            list(data.keys()),
        )
        return None
    from_unit_label = FROM_UNIT_LABELS.get(from_unit, "—")

    category = _normalize_id(_flow_pick(data, "destination_category"))
    if category not in DESTINATION_CATEGORY_LABELS:
        return None

    location = _flow_pick(data, "location_details", "location")
    destination = _normalize_id(_flow_pick(data, "destination"))

    if category in _MANUAL_CATEGORIES:
        if not location:
            return None
    elif category == "other_unit":
        expected = _OTHER_UNIT_DESTINATION_BY_FROM.get(from_unit)
        if not expected or destination != expected:
            return None
    elif category in _DROPDOWN_CATEGORIES:
        allowed = VALID_DESTINATIONS_BY_CATEGORY.get(category, frozenset())
        if destination not in allowed:
            return None
    else:
        return None

    vehicle_type = _normalize_vehicle_type(_flow_pick(data, "vehicle_type"))
    if vehicle_type not in VEHICLE_TYPE_LABELS:
        vehicle_type = "in_house"

    hire_type = ""
    if vehicle_type == "external_hire":
        hire_type = _normalize_id(_flow_pick(data, "hire_vehicle_type"))
        if hire_type not in HIRE_VEHICLE_TYPE_LABELS:
            hire_type = ""

    load_size = _normalize_id(_flow_pick(data, "load_size"))
    if load_size not in LOAD_SIZE_LABELS:
        logger.warning(
            "vehicle request parse failed: load_size=%r keys=%s",
            load_size,
            list(data.keys()),
        )
        return None

    required_time_raw = _flow_pick(data, "required_time")
    required_time = _normalize_required_time_hhmm(required_time_raw)
    if not required_time or required_time not in _allowed_required_time_slots():
        logger.warning(
            "vehicle request parse failed: required_time=%r keys=%s",
            required_time_raw,
            list(data.keys()),
        )
        return None

    required_at = _build_required_at({**data, "required_time": required_time})

    km = _estimated_distance_km(category, destination)
    return {
        "from_unit": from_unit,
        "from_unit_label": from_unit_label,
        "request_type": request_type,
        "request_type_label": REQUEST_TYPE_LABELS[request_type],
        "destination_category": category,
        "destination_category_label": DESTINATION_CATEGORY_LABELS[category],
        "location_details": location,
        "destination": destination,
        "destination_label": _destination_display(category, destination, location),
        "vehicle_type": vehicle_type,
        "vehicle_type_label": VEHICLE_TYPE_LABELS[vehicle_type],
        "hire_vehicle_type": hire_type,
        "hire_vehicle_type_label": HIRE_VEHICLE_TYPE_LABELS.get(hire_type, ""),
        "load_size": load_size,
        "load_size_label": LOAD_SIZE_LABELS[load_size],
        "estimated_distance_km": km,
        "estimated_distance_display": "—" if km is None else f"{km} KM",
        "required_time": required_time,
        "required_at": required_at,
    }


def _manager_approval_body(rd: dict) -> str:
    hire_line = ""
    if rd.get("hire_vehicle_type_label"):
        hire_line = f"Hire Vehicle Type: {rd['hire_vehicle_type_label']}\n"
    return (
        "Vehicle request\n\n"
        f"Requester: {_sentence_case_name(rd.get('employee_name') or '—')}\n"
        f"Department: {rd.get('department') or '—'}\n"
        f"From: {rd.get('from_unit_label') or '—'}\n"
        f"Request Type: {rd.get('request_type_label') or '—'}\n"
        f"Category: {rd.get('destination_category_label') or '—'}\n"
        f"Destination: {rd.get('destination_label') or '—'}\n"
        f"Vehicle Type: {rd.get('vehicle_type_label') or '—'}\n"
        f"{hire_line}"
        f"Load Capacity: {rd.get('load_size_label') or '—'}\n"
        f"Approx. Distance: {rd.get('estimated_distance_display') or '—'}\n"
        f"Required At: {rd.get('required_at') or '—'}"
    )


def _approval_template_values(rd: dict) -> dict[str, str]:
    hire = rd.get("hire_vehicle_type_label") or "—"
    return {
        "requester": _sentence_case_name(rd.get("employee_name") or "—"),
        "department": rd.get("department") or "—",
        "from": rd.get("from_unit_label") or "—",
        "request_type": rd.get("request_type_label") or "—",
        "category": rd.get("destination_category_label") or "—",
        "destination": rd.get("destination_label") or "—",
        "vehicle_type": rd.get("vehicle_type_label") or "—",
        "hire_vehicle_type": hire,
        "load_capacity": rd.get("load_size_label") or "—",
        "distance": rd.get("estimated_distance_display") or "—",
        "required_at": rd.get("required_at") or "—",
    }


def _template_body_values(rd: dict) -> list[str]:
    """Eleven body variables — must match approved Utility template {{1}}…{{11}}."""
    values = _approval_template_values(rd)
    fields = _approval_template_body_fields()
    if len(fields) != 11:
        logger.warning(
            "VEHICLE_REQUEST_APPROVAL_TEMPLATE_BODY_FIELDS should list exactly 11 "
            "fields for Utility template; got %s",
            len(fields),
        )
    return [values.get(key, "—")[:1024] for key in fields]


def _notify_logistics_manager(deps: VehicleRequestDeps, rd: dict, request_id: str) -> None:
    mgr = _logistics_manager_wa()
    if not mgr:
        logger.warning(
            "LOGISTICS_MANAGER_WHATSAPP_NUMBER not set — skip vehicle request notify"
        )
        return

    rid = (request_id or "").strip()
    body = _manager_approval_body(rd)
    assign_id = f"VEHICLE_ASSIGN_{rid}"[:256]
    cancel_id = f"VEHICLE_CANCEL_{rid}"[:256]
    buttons = [(assign_id, "Assign"), (cancel_id, "Cancel")]

    if deps.has_active_whatsapp_session(mgr):
        try:
            send_reply_buttons(
                wa_id_to_phone(mgr),
                body,
                buttons,
                callback_data=rid,
                ensure_contact=True,
                contact_name="Logistics Manager",
            )
            _set_pending_manager_notify(deps, mgr, rid)
            return
        except Exception:
            logger.exception(
                "vehicle request approval buttons failed request_id=%s", request_id
            )

    template_name = _approval_template_name()
    if template_name:
        try:
            ensure_customer(wa_id_to_phone(mgr), name="Logistics Manager")
            send_template(
                wa_id_to_phone(mgr),
                template_name,
                language_code=_approval_template_language(),
                body_values=_template_body_values(rd),
                callback_data=rid,
                ensure_contact=False,
            )
            logger.info(
                "vehicle request approval template sent request_id=%s (no active session)",
                request_id,
            )
            _set_pending_manager_notify(deps, mgr, rid)
            return
        except Exception:
            logger.exception(
                "vehicle request approval template failed request_id=%s", request_id
            )

    logger.info(
        "skip vehicle request notify request_id=%s (no manager session; set "
        "VEHICLE_REQUEST_APPROVAL_TEMPLATE_NAME for out-of-session notify)",
        request_id,
    )


def _employee_confirmation(rd: dict) -> str:
    hire_line = ""
    if rd.get("hire_vehicle_type_label"):
        hire_line = f"Hire vehicle: {rd['hire_vehicle_type_label']}\n"
    return (
        "Your vehicle request has been submitted.\n\n"
        f"From: {rd.get('from_unit_label') or '—'}\n"
        f"Type: {rd.get('request_type_label') or '—'}\n"
        f"Destination: {rd.get('destination_label') or '—'}\n"
        f"Distance: {rd.get('estimated_distance_display') or '—'}\n"
        f"Vehicle: {rd.get('vehicle_type_label') or '—'}\n"
        f"{hire_line}"
        f"Load: {rd.get('load_size_label') or '—'}\n"
        f"Required at: {rd.get('required_at') or '—'}"
    )


def _assign_options(db: object, vehicle_type: str) -> list[tuple[str, str]]:
    if _normalize_vehicle_type(vehicle_type) == "in_house":
        return _logistics_department_staff(db)
    return list(EXTERNAL_VENDORS)


def _assign_option_map(db: object, vehicle_type: str) -> dict[str, str]:
    return dict(_assign_options(db, vehicle_type))


def _fleet_vehicle_map() -> dict[str, str]:
    fleet = dict(INTERNAL_FLEET_VEHICLES)
    fleet["dost_3371"] = fleet["dost_3271"]
    return fleet


def _show_fleet_vehicle_list(
    sender: str,
    request_id: str,
    assignee_code: str,
    assignee_label: str,
    vehicle_type: str,
    deps: VehicleRequestDeps,
    *,
    mode: str = "assign",
) -> bool:
    options = list(INTERNAL_FLEET_VEHICLES)
    if not options:
        deps.send_to(sender, "No internal vehicles configured.")
        return True
    vtype = _normalize_vehicle_type(vehicle_type)
    is_reassign = (mode or "").strip().lower() == "reassign"
    state = SESSION_WAITING_VEHICLE_REASSIGN_FLEET if is_reassign else SESSION_WAITING_VEHICLE_FLEET_PICK
    session_fields = {
        "state": state,
        "vehicle_fleet_request_id": request_id,
        "vehicle_fleet_vehicle_type": vtype,
        "vehicle_fleet_assignee_code": assignee_code,
        "vehicle_fleet_assignee_label": assignee_label,
    }
    if is_reassign:
        session_fields["vehicle_reassign_request_id"] = request_id
        session_fields["vehicle_reassign_vehicle_type"] = vtype
    else:
        session_fields["vehicle_assign_request_id"] = request_id
        session_fields["vehicle_assign_vehicle_type"] = vtype
    deps.session_merge(sender, **session_fields)
    list_rows = [
        {
            "id": f"VFLEET_{request_id}_{code}"[:256],
            "title": label[:24],
        }
        for code, label in options
    ]
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            f"Select vehicle for {_sentence_case_name(assignee_label)}:",
            list_rows,
            button_label="Vehicle",
            section_title="Vehicles",
            callback_data=request_id,
        )
    except Exception:
        logger.exception("vehicle fleet list failed request_id=%s", request_id)
        numbered: dict[str, str] = {}
        lines: list[str] = []
        for idx, (code, label) in enumerate(options, start=1):
            numbered[str(idx)] = code
            lines.append(f"{idx}. {label}")
        deps.session_merge(sender, vehicle_fleet_options=numbered, **session_fields)
        deps.send_to(
            sender,
            "Select vehicle — reply with the number:\n" + "\n".join(lines),
        )
    return True


def _parse_vfleet(incoming: str, *, request_id_hint: str = "") -> tuple[str, str] | None:
    raw = (incoming or "").strip()
    if not raw.upper().startswith("VFLEET_"):
        return None
    rid_hint = (request_id_hint or "").strip()
    if rid_hint:
        prefix = f"VFLEET_{rid_hint}_"
        if raw.startswith(prefix):
            code = raw[len(prefix):].strip().lower()
            if code:
                return rid_hint, code
    body = raw[7:]
    parts = body.split("_", 1)
    if len(parts) != 2:
        return None
    rid, code = parts[0].strip(), parts[1].strip().lower()
    if rid and code:
        return rid, code
    return None


def _complete_vehicle_assignment(
    sender: str,
    request_id: str,
    rd: dict,
    ref: object,
    *,
    vtype: str,
    assignee_code: str,
    assignee_label: str,
    staff_wa: str,
    fleet_vehicle_code: str = "",
    fleet_vehicle_label: str = "",
    reassign: bool = False,
    old_wa: str = "",
    old_name: str = "",
    deps: VehicleRequestDeps,
) -> None:
    is_internal = vtype == "in_house"
    update = {
        "vehicle_request_status": "ASSIGNED",
        "vehicle_type": vtype,
        "vehicle_type_label": VEHICLE_TYPE_LABELS.get(vtype, vtype),
        "assigned_to": assignee_label,
        "assigned_to_code": assignee_code,
        "assigned_to_wa": staff_wa,
        "assigned_by": sender,
        "assigned_at": deps.utcnow(),
        "assignee_can_start": is_internal,
        "is_active_trip": False,
    }
    if fleet_vehicle_code:
        update["fleet_vehicle_code"] = fleet_vehicle_code
        update["fleet_vehicle_label"] = fleet_vehicle_label
    elif not is_internal:
        update["fleet_vehicle_code"] = ""
        update["fleet_vehicle_label"] = ""
    if reassign:
        update["previous_assignee"] = old_name
        update["previous_assignee_code"] = _normalize_id(rd.get("assigned_to_code") or "")
        update["previous_assignee_wa"] = old_wa
        update["reassigned_at"] = deps.utcnow()
    ref.update(update)
    updated = ref.get().to_dict() or rd
    _notify_internal_assignee(
        deps,
        updated,
        assignee_code=assignee_code,
        assignee_label=assignee_label,
        request_id=request_id,
    )
    assignee_display = _sentence_case_name(assignee_label)
    vehicle_line = f"\nVehicle: {fleet_vehicle_label}" if fleet_vehicle_label else ""
    if reassign and old_wa:
        deps.send_to(
            old_wa,
            f"The request has been re-assigned to {assignee_display}. Thanks.",
        )
    employee = (rd.get("employee") or "").strip()
    if employee:
        if reassign:
            msg = f"Your vehicle request has been re-assigned to {assignee_display}."
        else:
            msg = f"Your vehicle request has been assigned to {assignee_display}.{vehicle_line}"
        deps.send_to(employee, msg)
    if reassign:
        deps.send_to(sender, f"Re-assigned to {assignee_display}.{vehicle_line}")
    else:
        deps.send_to(sender, f"Assigned to {assignee_display}.{vehicle_line}")
    deps.clear_session(sender)


def _is_internal_type_label(raw: str) -> bool:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key in ("internal", "in_house", "company_vehicle")


def _is_external_type_label(raw: str) -> bool:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key in ("external", "external_hire", "hire", "external_vehicle")


def _vehicle_type_from_type_pick(incoming: str) -> str | None:
    raw = (incoming or "").strip()
    upper = raw.upper()
    if upper.startswith("VTYPE_INT_"):
        return "in_house"
    if upper.startswith("VTYPE_EXT_"):
        return "external_hire"
    if _is_internal_type_label(raw):
        return "in_house"
    if _is_external_type_label(raw):
        return "external_hire"
    return None


def _request_id_from_type_pick(incoming: str) -> str:
    raw = (incoming or "").strip()
    upper = raw.upper()
    for prefix in ("VTYPE_INT_", "VTYPE_EXT_"):
        if upper.startswith(prefix):
            return raw[len(prefix) :].strip()
    return ""


def _assign_type_pick_session(sender: str, deps: VehicleRequestDeps) -> dict:
    snap = deps.session_ref(sender).get()
    if not snap.exists:
        return {}
    data = snap.to_dict() or {}
    if data.get("state") != SESSION_WAITING_VEHICLE_ASSIGN_TYPE:
        return {}
    return data


def _resolve_assign_type_pick(
    incoming: str,
    sender: str,
    deps: VehicleRequestDeps,
    *,
    callback_request_id: str = "",
) -> tuple[str, str] | None:
    """Return (request_id, in_house|external_hire) when manager picks Internal/External."""
    vtype = _vehicle_type_from_type_pick(incoming)
    if not vtype:
        return None
    rid = _request_id_from_type_pick(incoming)
    if not rid:
        rid = (callback_request_id or "").strip()
    if not rid:
        rid = (_assign_type_pick_session(sender, deps).get("vehicle_type_pick_request_id") or "").strip()
    if not rid:
        return None
    return rid, vtype


def _prompt_vehicle_type_choice(
    sender: str,
    request_id: str,
    deps: VehicleRequestDeps,
    *,
    mode: str,
) -> None:
    """Ask Internal vs External before showing assignee list."""
    rid = (request_id or "").strip()
    deps.session_merge(
        sender,
        state=SESSION_WAITING_VEHICLE_ASSIGN_TYPE,
        vehicle_type_pick_request_id=rid,
        vehicle_type_pick_mode=(mode or "assign").strip().lower(),
    )
    int_id = f"VTYPE_INT_{rid}"[:256]
    ext_id = f"VTYPE_EXT_{rid}"[:256]
    try:
        send_reply_buttons(
            wa_id_to_phone(sender),
            "Select vehicle type:",
            [(int_id, "Internal"), (ext_id, "External")],
            callback_data=rid,
            ensure_contact=True,
            contact_name="Logistics Manager",
        )
    except Exception:
        logger.exception("vehicle type choice failed request_id=%s", rid)
        deps.send_to(
            sender,
            "Select vehicle type:\n1. Internal\n2. External\n\nReply 1 or 2.",
        )
        deps.session_merge(
            sender,
            state=SESSION_WAITING_VEHICLE_ASSIGN_TYPE,
            vehicle_type_pick_request_id=rid,
            vehicle_type_pick_mode=(mode or "assign").strip().lower(),
            vehicle_type_pick_numeric=True,
        )


def _show_assignee_list(
    sender: str,
    request_id: str,
    vehicle_type: str,
    deps: VehicleRequestDeps,
) -> bool:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Vehicle request not found.")
        return True
    _ref, rd = loaded
    if _request_status(rd) != "PENDING":
        deps.send_to(sender, f"Request already {_request_status(rd).lower()}.")
        return True

    vtype = _normalize_vehicle_type(vehicle_type)
    options = _assign_options(deps.db, vtype)
    if not options:
        label = "internal staff" if vtype == "in_house" else "external vendors"
        deps.send_to(
            sender,
            f"No {label} available.\nPlease update users in Firestore or contact admin.",
        )
        return True

    type_label = VEHICLE_TYPE_LABELS.get(vtype, vtype)
    rows = [
        (f"VASSIGN_{request_id}_{code}"[:256], label)
        for code, label in options
    ]
    list_rows = [{"id": rid, "title": _sentence_case_name(label)[:24]} for rid, label in rows]
    deps.session_merge(
        sender,
        state=SESSION_WAITING_VEHICLE_ASSIGN,
        vehicle_assign_request_id=request_id,
        vehicle_assign_vehicle_type=vtype,
    )
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            f"Select {type_label.lower()} to assign:",
            list_rows,
            button_label="Assign",
            section_title="Assign",
            callback_data=request_id,
        )
    except Exception:
        logger.exception("vehicle assign list failed request_id=%s", request_id)
        numbered: dict[str, str] = {}
        lines: list[str] = []
        for idx, (code, label) in enumerate(options, start=1):
            numbered[str(idx)] = code
            lines.append(f"{idx}. {_sentence_case_name(label)}")
        deps.session_merge(
            sender,
            state=SESSION_WAITING_VEHICLE_ASSIGN,
            vehicle_assign_request_id=request_id,
            vehicle_assign_vehicle_type=vtype,
            vehicle_assign_options=numbered,
        )
        deps.send_to(
            sender,
            f"Select {type_label.lower()} to assign — reply with the number:\n"
            + "\n".join(lines),
        )
    return True


def _show_reassignee_list(
    sender: str,
    request_id: str,
    vehicle_type: str,
    deps: VehicleRequestDeps,
) -> bool:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Vehicle request not found.")
        return True
    _ref, rd = loaded
    if _trip_started(rd):
        deps.send_to(
            sender,
            "This trip has already started. Re Assign is not allowed.",
        )
        deps.clear_session(sender)
        return True
    if _request_status(rd) != "ASSIGNED":
        deps.send_to(sender, "Only assigned requests can be re-assigned.")
        deps.clear_session(sender)
        return True

    vtype = _normalize_vehicle_type(vehicle_type)
    current_code = _normalize_id(rd.get("assigned_to_code") or "")
    options = [
        (code, label)
        for code, label in _assign_options(deps.db, vtype)
        if code != current_code
    ]
    if not options:
        deps.send_to(sender, "No other assignee available for this type.")
        return True

    type_label = VEHICLE_TYPE_LABELS.get(vtype, vtype)
    list_rows = [
        {
            "id": f"VREASSIGN_{request_id}_{code}"[:256],
            "title": _sentence_case_name(label)[:24],
        }
        for code, label in options
    ]
    deps.session_merge(
        sender,
        state=SESSION_WAITING_VEHICLE_REASSIGN,
        vehicle_reassign_request_id=request_id,
        vehicle_reassign_vehicle_type=vtype,
    )
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            f"Select new {type_label.lower()} assignee:",
            list_rows,
            button_label="Re Assign",
            section_title="Assignee",
            callback_data=request_id,
        )
    except Exception:
        logger.exception("vehicle reassign list failed request_id=%s", request_id)
        numbered: dict[str, str] = {}
        lines: list[str] = []
        for idx, (code, label) in enumerate(options, start=1):
            numbered[str(idx)] = code
            lines.append(f"{idx}. {_sentence_case_name(label)}")
        deps.session_merge(
            sender,
            state=SESSION_WAITING_VEHICLE_REASSIGN,
            vehicle_reassign_request_id=request_id,
            vehicle_reassign_vehicle_type=vtype,
            vehicle_reassign_options=numbered,
        )
        deps.send_to(
            sender,
            f"Select new {type_label.lower()} assignee — reply with the number:\n"
            + "\n".join(lines),
        )
    return True


def handle_vehicle_assign_type_pick(
    sender: str,
    incoming: str,
    session: dict,
    deps: VehicleRequestDeps,
    *,
    callback_request_id: str = "",
) -> None:
    if not is_logistics_manager(sender, deps.same_whatsapp):
        deps.clear_session(sender)
        return
    session_data = session or _assign_type_pick_session(sender, deps)
    if (session_data.get("vehicle_type_pick_numeric")
        and (incoming or "").strip() == "1"):
        vtype = "in_house"
        rid = (session_data.get("vehicle_type_pick_request_id") or "").strip()
    elif session_data.get("vehicle_type_pick_numeric") and (incoming or "").strip() == "2":
        vtype = "external_hire"
        rid = (session_data.get("vehicle_type_pick_request_id") or "").strip()
    else:
        resolved = _resolve_assign_type_pick(
            incoming, sender, deps, callback_request_id=callback_request_id
        )
        if not resolved:
            deps.send_to(sender, "Please choose Internal or External.")
            return
        rid, vtype = resolved
    mode = (session_data.get("vehicle_type_pick_mode") or "assign").strip().lower()
    if mode == "reassign":
        _show_reassignee_list(sender, rid, vtype, deps)
    else:
        _show_assignee_list(sender, rid, vtype, deps)


def _parse_vassign(
    incoming: str, *, request_id_hint: str = ""
) -> tuple[str, str] | None:
    raw = (incoming or "").strip()
    if not raw.upper().startswith("VASSIGN_"):
        return None

    rid_hint = (request_id_hint or "").strip()
    if rid_hint:
        prefix = f"VASSIGN_{rid_hint}_"
        if raw.startswith(prefix):
            code = raw[len(prefix) :].strip().lower()
            if code:
                return rid_hint, code

    rest = raw[8:]
    for code in sorted(_EXTERNAL_VENDOR_CODES, key=len, reverse=True):
        suffix = f"_{code}"
        if rest.lower().endswith(suffix.lower()):
            request_id = rest[: -len(suffix)]
            if request_id:
                return request_id, code
    return None


def _parse_vehicle_action(incoming: str, prefix: str) -> str | None:
    raw = (incoming or "").strip()
    upper = raw.upper()
    p = prefix.upper()
    if not upper.startswith(p):
        return None
    rid = raw[len(prefix) :].strip()
    return rid or None


def _is_assign_label(raw: str) -> bool:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key in ("assign", "assign_vehicle", "assign_request")


def _is_cancel_label(raw: str) -> bool:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key in ("cancel", "cancelled", "cancel_request")


def _is_reassign_label(raw: str) -> bool:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key in ("re_assign", "reassign", "re_assign_trip", "reassign_trip")


def _pending_manage_action_request_id(sender: str, deps: VehicleRequestDeps) -> str:
    snap = deps.session_ref(sender).get()
    if not snap.exists:
        return ""
    data = snap.to_dict() or {}
    if data.get("state") != SESSION_WAITING_VEHICLE_MANAGE_ACTION:
        return ""
    return (data.get("vehicle_manage_request_id") or "").strip()


def _resolve_manage_action_request_id(
    incoming: str,
    sender: str,
    deps: VehicleRequestDeps,
    *,
    callback_request_id: str = "",
    want: str,
) -> str | None:
    """Map Manage Quick Reply (Re Assign/Cancel) + callbackData to request id."""
    raw = (incoming or "").strip()
    if want == "reassign" and not _is_reassign_label(raw):
        return None
    if want == "cancel" and not _is_cancel_label(raw):
        return None
    cb_rid = (callback_request_id or "").strip()
    if cb_rid:
        return cb_rid
    return _pending_manage_action_request_id(sender, deps) or None


def _pending_manager_notify_request_id(sender: str, deps: VehicleRequestDeps) -> str:
    snap = deps.session_ref(sender).get()
    if not snap.exists:
        return ""
    data = snap.to_dict() or {}
    if data.get("state") != SESSION_WAITING_VEHICLE_MANAGER_NOTIFY:
        return ""
    return (data.get("vehicle_manager_request_id") or "").strip()


def _set_pending_manager_notify(
    deps: VehicleRequestDeps, manager_wa: str, request_id: str
) -> None:
    deps.session_merge(
        manager_wa,
        state=SESSION_WAITING_VEHICLE_MANAGER_NOTIFY,
        vehicle_manager_request_id=(request_id or "").strip(),
    )


def _resolve_manager_template_request_id(
    incoming: str,
    sender: str,
    deps: VehicleRequestDeps,
    *,
    callback_request_id: str = "",
    want: str,
) -> str | None:
    """Map template Quick Reply (Assign/Cancel) + callbackData to request id."""
    raw = (incoming or "").strip()
    if want == "assign" and not _is_assign_label(raw):
        return None
    if want == "cancel" and not _is_cancel_label(raw):
        return None
    cb_rid = (callback_request_id or "").strip()
    if cb_rid:
        return cb_rid
    return _pending_manager_notify_request_id(sender, deps) or None


def try_start_form(sender: str, deps: VehicleRequestDeps) -> None:
    if is_logistics_manager(sender, deps.same_whatsapp):
        try_start_manage(sender, deps)
        return

    exists, ud = get_user_record(sender)
    if not exists or not ud:
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return
    if not can_raise_vehicle_request(
        ud, sender=sender, same_whatsapp=deps.same_whatsapp
    ):
        deps.send_to(
            sender,
            "Vehicle Request is not available for your account.\n"
            "Only logistics requesters can raise vehicle requests.",
        )
        return

    if not vehicle_request_flow_enabled():
        deps.send_to(
            sender,
            "Vehicle request form is not configured yet.\nPlease contact admin.",
        )
        return

    name = ud.get("name") or "Employee"
    if send_vehicle_request_flow_form(wa_id_to_phone(sender), employee_name=name):
        return
    logger.warning("vehicle request flow template send failed sender=%s", sender)
    deps.send_to(
        sender,
        "Could not open Vehicle Request form. Please try again or contact admin.",
    )


def handle_flow_submission(
    sender: str, response_json: dict | str | None, deps: VehicleRequestDeps
) -> None:
    parsed = parse_flow_response(response_json)
    if not parsed:
        logger.warning(
            "vehicle request flow submission unreadable sender=%s payload=%s",
            sender,
            response_json,
        )
        deps.send_to(
            sender,
            "Could not read the Vehicle Request form. Please submit again or contact admin.",
        )
        return

    if is_logistics_manager(sender, deps.same_whatsapp):
        deps.send_to(sender, "Logistics manager cannot raise vehicle requests.")
        return

    exists, ud = get_user_record(sender)
    if not exists or not ud:
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return
    if not can_raise_vehicle_request(
        ud, sender=sender, same_whatsapp=deps.same_whatsapp
    ):
        deps.send_to(
            sender,
            "Vehicle Request is not available for your account.",
        )
        return

    ref = deps.db.collection("requests").document()
    request_id = ref.id
    now = deps.utcnow()
    reason = (
        f"{parsed['request_type_label']} — "
        f"{parsed['destination_label']} ({parsed['load_size_label']})"
    )
    payload = {
        "request_id": request_id,
        "requested_datetime": now,
        "employee": sender,
        "employee_id": ud.get("employee_id") or "",
        "employee_name": ud.get("name") or "Employee",
        "department": ud.get("department") or "",
        "from_unit": parsed["from_unit"],
        "from_unit_label": parsed["from_unit_label"],
        "type": "VEHICLE_REQUEST",
        "reason": reason,
        "request_type": parsed["request_type"],
        "request_type_label": parsed["request_type_label"],
        "destination_category": parsed["destination_category"],
        "destination_category_label": parsed["destination_category_label"],
        "location_details": parsed["location_details"],
        "destination": parsed["destination"],
        "destination_label": parsed["destination_label"],
        "vehicle_type": parsed["vehicle_type"],
        "vehicle_type_label": parsed["vehicle_type_label"],
        "hire_vehicle_type": parsed["hire_vehicle_type"],
        "hire_vehicle_type_label": parsed["hire_vehicle_type_label"],
        "load_size": parsed["load_size"],
        "load_size_label": parsed["load_size_label"],
        "estimated_distance_km": parsed["estimated_distance_km"],
        "estimated_distance_display": parsed["estimated_distance_display"],
        "required_time": parsed["required_time"],
        "required_at": parsed["required_at"],
        "vehicle_request_status": "PENDING",
        "submission_source": "whatsapp_flow",
        "manager_status": "N/A",
        "jmd_status": "N/A",
        "md_status": "N/A",
        "source": "whatsapp_request",
    }
    ref.set(payload)
    _notify_logistics_manager(deps, payload, request_id)
    deps.send_to(sender, _employee_confirmation(payload))
    logger.info("vehicle request submitted request_id=%s employee=%s", request_id, sender)


def handle_logistics_manager_gate(
    sender: str,
    incoming: str,
    deps: VehicleRequestDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not is_logistics_manager(sender, deps.same_whatsapp):
        return False

    type_pick = _resolve_assign_type_pick(
        incoming, sender, deps, callback_request_id=callback_request_id
    )
    if type_pick:
        rid, vtype = type_pick
        sess = _assign_type_pick_session(sender, deps)
        mode = (sess.get("vehicle_type_pick_mode") or "assign").strip().lower()
        if mode == "reassign":
            return _show_reassignee_list(sender, rid, vtype, deps)
        return _show_assignee_list(sender, rid, vtype, deps)

    request_id = _parse_vehicle_action(incoming, "VEHICLE_ASSIGN_")
    if not request_id:
        request_id = _resolve_manager_template_request_id(
            incoming,
            sender,
            deps,
            callback_request_id=callback_request_id,
            want="assign",
        )
    if request_id:
        return _handle_assign_click(sender, request_id, deps)

    request_id = _parse_vehicle_action(incoming, "VEHICLE_CANCEL_")
    if not request_id:
        request_id = _resolve_manager_template_request_id(
            incoming,
            sender,
            deps,
            callback_request_id=callback_request_id,
            want="cancel",
        )
    if request_id:
        return _handle_cancel_click(sender, request_id, deps)

    return False


def _handle_assign_click(sender: str, request_id: str, deps: VehicleRequestDeps) -> bool:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Vehicle request not found.")
        return True
    _ref, rd = loaded
    if _request_status(rd) != "PENDING":
        deps.send_to(sender, f"Request already {_request_status(rd).lower()}.")
        return True
    _prompt_vehicle_type_choice(sender, request_id, deps, mode="assign")
    return True


def _handle_cancel_click(sender: str, request_id: str, deps: VehicleRequestDeps) -> bool:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Vehicle request not found.")
        return True
    ref, rd = loaded
    status = _request_status(rd)
    if status != "PENDING":
        deps.send_to(sender, f"Request already {status.lower()}.")
        return True

    ref.update({
        "vehicle_request_status": "CANCELLED",
        "cancelled_by": sender,
        "cancelled_at": deps.utcnow(),
    })
    employee = (rd.get("employee") or "").strip()
    if employee:
        deps.send_to(
            employee,
            "Your vehicle request has been cancelled by logistics.",
        )
    deps.send_to(sender, "Vehicle request cancelled.")
    deps.clear_session(sender)
    return True


def handle_vehicle_assign_pick(
    sender: str, incoming: str, session: dict, deps: VehicleRequestDeps
) -> None:
    if not is_logistics_manager(sender, deps.same_whatsapp):
        deps.clear_session(sender)
        deps.send_to(sender, "Not authorized.")
        return

    session_rid = (session or {}).get("vehicle_assign_request_id") or ""
    parsed = _parse_vassign(incoming, request_id_hint=session_rid)
    assignee_code = ""
    request_id = session_rid

    if parsed:
        request_id, assignee_code = parsed
    else:
        pick = (incoming or "").strip()
        opts = (session or {}).get("vehicle_assign_options") or {}
        if pick.isdigit() and pick in opts and session_rid:
            assignee_code = str(opts[pick]).strip().lower()
        else:
            deps.send_to(sender, "Please pick a driver or transport from the list.")
            return

    if not assignee_code:
        deps.send_to(sender, "Please pick a driver or transport from the list.")
        return
    if session_rid and request_id != session_rid:
        deps.send_to(sender, "Session expired. Tap Assign on the request again.")
        deps.clear_session(sender)
        return

    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.clear_session(sender)
        deps.send_to(sender, "Vehicle request not found.")
        return
    ref, rd = loaded
    if _request_status(rd) != "PENDING":
        deps.clear_session(sender)
        deps.send_to(sender, f"Request already {_request_status(rd).lower()}.")
        return

    allowed = _assign_option_map(
        deps.db,
        (session or {}).get("vehicle_assign_vehicle_type")
        or rd.get("vehicle_type")
        or "",
    )
    assignee_label = allowed.get(assignee_code)
    if not assignee_label:
        deps.send_to(sender, "Invalid selection.")
        return

    vtype = _normalize_vehicle_type(
        (session or {}).get("vehicle_assign_vehicle_type")
        or rd.get("vehicle_type")
        or "in_house"
    )
    is_internal = vtype == "in_house"
    if is_internal:
        _show_fleet_vehicle_list(
            sender,
            request_id,
            assignee_code,
            assignee_label,
            vtype,
            deps,
            mode="assign",
        )
        return

    staff = _staff_wa_for_assignee_code(deps.db, assignee_code)
    staff_wa = staff[0] if staff else ""
    _complete_vehicle_assignment(
        sender,
        request_id,
        rd,
        ref,
        vtype=vtype,
        assignee_code=assignee_code,
        assignee_label=assignee_label,
        staff_wa=staff_wa,
        deps=deps,
    )


def handle_vehicle_fleet_pick(
    sender: str, incoming: str, session: dict, deps: VehicleRequestDeps
) -> None:
    if not is_logistics_manager(sender, deps.same_whatsapp):
        deps.clear_session(sender)
        deps.send_to(sender, "Not authorized.")
        return
    session_rid = (session or {}).get("vehicle_fleet_request_id") or ""
    parsed = _parse_vfleet(incoming, request_id_hint=session_rid)
    fleet_code = ""
    request_id = session_rid
    if parsed:
        request_id, fleet_code = parsed
    else:
        pick = (incoming or "").strip()
        opts = (session or {}).get("vehicle_fleet_options") or {}
        if pick.isdigit() and pick in opts and session_rid:
            fleet_code = str(opts[pick]).strip().lower()
    fleet_map = _fleet_vehicle_map()
    fleet_label = fleet_map.get(fleet_code, "")
    if not fleet_code or not fleet_label:
        deps.send_to(sender, "Please pick a vehicle from the list.")
        return
    assignee_code = (session or {}).get("vehicle_fleet_assignee_code") or ""
    assignee_label = (session or {}).get("vehicle_fleet_assignee_label") or ""
    if not assignee_code or not assignee_label:
        deps.clear_session(sender)
        deps.send_to(sender, "Session expired. Tap Assign on the request again.")
        return
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.clear_session(sender)
        deps.send_to(sender, "Vehicle request not found.")
        return
    ref, rd = loaded
    if _request_status(rd) != "PENDING":
        deps.clear_session(sender)
        deps.send_to(sender, f"Request already {_request_status(rd).lower()}.")
        return
    vtype = _normalize_vehicle_type(
        (session or {}).get("vehicle_fleet_vehicle_type") or "in_house"
    )
    staff = _staff_wa_for_assignee_code(deps.db, assignee_code)
    staff_wa = staff[0] if staff else ""
    _complete_vehicle_assignment(
        sender,
        request_id,
        rd,
        ref,
        vtype=vtype,
        assignee_code=assignee_code,
        assignee_label=assignee_label,
        staff_wa=staff_wa,
        fleet_vehicle_code=fleet_code,
        fleet_vehicle_label=fleet_label,
        deps=deps,
    )


def _fetch_today_vehicle_requests(db: object) -> list[tuple[str, dict]]:
    """Today's vehicle requests with status Assigned (Manage list)."""
    today = _ist_now().date()
    rows: list[tuple[str, dict]] = []
    try:
        snaps = db.collection("requests").where("type", "==", "VEHICLE_REQUEST").stream()
    except Exception:
        logger.exception("vehicle manage list query failed")
        return rows
    for snap in snaps:
        rd = snap.to_dict() or {}
        if _request_status(rd) != "ASSIGNED":
            continue
        if not _request_on_ist_day(rd, today):
            continue
        rows.append((snap.id, rd))
    rows.sort(
        key=lambda item: item[1].get("requested_datetime") or "",
        reverse=True,
    )
    return rows


def try_start_manage(sender: str, deps: VehicleRequestDeps) -> None:
    if not is_logistics_manager(sender, deps.same_whatsapp):
        deps.send_to(sender, "Not authorized.")
        return
    rows = _fetch_today_vehicle_requests(deps.db)
    if not rows:
        deps.send_to(
            sender,
            "No assigned vehicle requests for today.\n"
            "New requests will arrive here with Assign / Cancel buttons.",
        )
        return
    list_rows = [_manage_list_row_fields(rid, rd) for rid, rd in rows[:10]]
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            "Assigned requests for today:",
            list_rows,
            button_label="Manage",
            section_title="Today",
            callback_data="vehicle-manage",
        )
    except Exception:
        logger.exception("vehicle manage list failed sender=%s", sender)
        lines = "\n".join(f"• {_manage_row_title(rd)}" for _rid, rd in rows[:10])
        deps.send_to(sender, f"Assigned requests for today:\n{lines}")


def _parse_manage_pick(incoming: str) -> str | None:
    return _parse_vehicle_action(incoming, "VMANAGE_")


def _send_manage_actions(
    sender: str, deps: VehicleRequestDeps, request_id: str, rd: dict
) -> None:
    status = _request_status(rd)
    if status != "ASSIGNED":
        if status == "PENDING":
            deps.send_to(
                sender,
                f"{_manage_row_title(rd)}\n\n"
                "Status: Pending.\n"
                "Use Assign or Cancel on the approval message.",
            )
        elif status == "STARTED":
            deps.send_to(
                sender,
                f"{_manage_row_title(rd)}\n\n"
                "This trip has already started.\n"
                "Re Assign and Cancel are not allowed.",
            )
        else:
            deps.send_to(
                sender,
                f"{_manage_row_title(rd)}\n\n"
                f"Status: {status.lower()}.\n"
                "Re Assign and Cancel are only for Assigned requests.",
            )
        deps.clear_session(sender)
        return

    buttons = [
        (f"VMREASSIGN_{request_id}"[:256], "Re Assign"),
        (f"VMCANCEL_{request_id}"[:256], "Cancel"),
    ]
    body = _manage_row_title(rd)
    deps.session_merge(
        sender,
        state=SESSION_WAITING_VEHICLE_MANAGE_ACTION,
        vehicle_manage_request_id=request_id,
    )
    try:
        send_reply_buttons(
            wa_id_to_phone(sender),
            body,
            buttons,
            callback_data=request_id,
        )
    except Exception:
        logger.exception("vehicle manage actions failed request_id=%s", request_id)
        deps.send_to(sender, body + "\n\nUse the buttons above or send Hi to retry.")


def handle_manager_manage_gate(
    sender: str,
    incoming: str,
    deps: VehicleRequestDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not is_logistics_manager(sender, deps.same_whatsapp):
        return False

    if incoming.strip().upper() in ("VEHICLE_MANAGE", "MANAGE"):
        try_start_manage(sender, deps)
        return True

    request_id = _parse_manage_pick(incoming)
    if request_id:
        loaded = _load_request(deps.db, request_id)
        if not loaded:
            deps.send_to(sender, "Vehicle request not found.")
            return True
        _ref, rd = loaded
        _send_manage_actions(sender, deps, request_id, rd)
        return True

    request_id = _parse_vehicle_action(incoming, "VMCANCEL_")
    if not request_id:
        request_id = _resolve_manage_action_request_id(
            incoming,
            sender,
            deps,
            callback_request_id=callback_request_id,
            want="cancel",
        )
    if request_id:
        return _handle_manage_cancel(sender, request_id, deps)

    request_id = _parse_vehicle_action(incoming, "VMREASSIGN_")
    if not request_id:
        request_id = _resolve_manage_action_request_id(
            incoming,
            sender,
            deps,
            callback_request_id=callback_request_id,
            want="reassign",
        )
    if request_id:
        return _handle_manage_reassign_click(sender, request_id, deps)

    return False


def handle_manager_manage_action(
    sender: str,
    incoming: str,
    session: dict,
    deps: VehicleRequestDeps,
    *,
    callback_request_id: str = "",
) -> None:
    if not is_logistics_manager(sender, deps.same_whatsapp):
        deps.clear_session(sender)
        return
    session_rid = (session or {}).get("vehicle_manage_request_id") or ""
    request_id = _parse_vehicle_action(incoming, "VMCANCEL_")
    if not request_id:
        request_id = _resolve_manage_action_request_id(
            incoming,
            sender,
            deps,
            callback_request_id=callback_request_id,
            want="cancel",
        )
    if request_id and (not session_rid or request_id == session_rid):
        _handle_manage_cancel(sender, request_id, deps)
        return
    request_id = _parse_vehicle_action(incoming, "VMREASSIGN_")
    if not request_id:
        request_id = _resolve_manage_action_request_id(
            incoming,
            sender,
            deps,
            callback_request_id=callback_request_id,
            want="reassign",
        )
    if request_id and (not session_rid or request_id == session_rid):
        _handle_manage_reassign_click(sender, request_id, deps)
        return
    deps.send_to(sender, "Please use Re Assign or Cancel.")


def _handle_manage_cancel(
    sender: str, request_id: str, deps: VehicleRequestDeps
) -> bool:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Vehicle request not found.")
        return True
    ref, rd = loaded
    if _trip_started(rd):
        deps.send_to(
            sender,
            "This trip has already started. Cancel is not allowed.",
        )
        deps.clear_session(sender)
        return True
    status = _request_status(rd)
    if status != "ASSIGNED":
        deps.send_to(
            sender,
            "Cancel from Manage is only allowed when status is Assigned.",
        )
        deps.clear_session(sender)
        return True

    ref.update({
        "vehicle_request_status": "CANCELLED",
        "cancelled_by": sender,
        "cancelled_at": deps.utcnow(),
        "assignee_can_start": False,
        "is_active_trip": False,
    })
    assignee_wa = (rd.get("assigned_to_wa") or "").strip()
    if assignee_wa:
        deps.send_to(
            assignee_wa,
            "Your assigned vehicle request has been cancelled by logistics.",
        )
    employee = (rd.get("employee") or "").strip()
    if employee:
        deps.send_to(
            employee,
            "Your vehicle request has been cancelled by logistics.",
        )
    deps.send_to(sender, "Vehicle request cancelled.")
    deps.clear_session(sender)
    return True


def _handle_manage_reassign_click(
    sender: str, request_id: str, deps: VehicleRequestDeps
) -> bool:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Vehicle request not found.")
        return True
    _ref, rd = loaded
    if _trip_started(rd):
        deps.send_to(
            sender,
            "This trip has already started. Re Assign is not allowed.",
        )
        deps.clear_session(sender)
        return True
    if _request_status(rd) != "ASSIGNED":
        deps.send_to(sender, "Only assigned requests can be re-assigned.")
        deps.clear_session(sender)
        return True
    _prompt_vehicle_type_choice(sender, request_id, deps, mode="reassign")
    return True


def _parse_vreassign(
    incoming: str, *, request_id_hint: str = ""
) -> tuple[str, str] | None:
    raw = (incoming or "").strip()
    if not raw.upper().startswith("VREASSIGN_"):
        return None
    rid_hint = (request_id_hint or "").strip()
    if rid_hint:
        prefix = f"VREASSIGN_{rid_hint}_"
        if raw.startswith(prefix):
            code = raw[len(prefix) :].strip().lower()
            if code:
                return rid_hint, code
    rest = raw[10:]
    for code in sorted(_EXTERNAL_VENDOR_CODES, key=len, reverse=True):
        suffix = f"_{code}"
        if rest.lower().endswith(suffix.lower()):
            request_id = rest[: -len(suffix)]
            if request_id:
                return request_id, code
    return None


def handle_vehicle_reassign_pick(
    sender: str, incoming: str, session: dict, deps: VehicleRequestDeps
) -> None:
    if not is_logistics_manager(sender, deps.same_whatsapp):
        deps.clear_session(sender)
        return
    session_rid = (session or {}).get("vehicle_reassign_request_id") or ""
    parsed = _parse_vreassign(incoming, request_id_hint=session_rid)
    assignee_code = ""
    request_id = session_rid

    if parsed:
        request_id, assignee_code = parsed
    else:
        pick = (incoming or "").strip()
        opts = (session or {}).get("vehicle_reassign_options") or {}
        if pick.isdigit() and pick in opts and session_rid:
            assignee_code = str(opts[pick]).strip().lower()
        else:
            deps.send_to(sender, "Please pick a new assignee from the list.")
            return

    if not assignee_code:
        deps.send_to(sender, "Please pick a new assignee from the list.")
        return
    if session_rid and request_id != session_rid:
        deps.clear_session(sender)
        deps.send_to(sender, "Session expired. Try Manage again.")
        return

    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.clear_session(sender)
        deps.send_to(sender, "Vehicle request not found.")
        return
    ref, rd = loaded
    if _request_status(rd) != "ASSIGNED":
        deps.clear_session(sender)
        deps.send_to(sender, "Request cannot be re-assigned now.")
        return

    old_code = _normalize_id(rd.get("assigned_to_code") or "")
    if assignee_code == old_code:
        deps.send_to(sender, "Choose a different assignee.")
        return

    allowed = _assign_option_map(
        deps.db,
        (session or {}).get("vehicle_reassign_vehicle_type")
        or rd.get("vehicle_type")
        or "",
    )
    assignee_label = allowed.get(assignee_code)
    if not assignee_label:
        deps.send_to(sender, "Invalid selection.")
        return

    old_wa = (rd.get("assigned_to_wa") or "").strip()
    old_name = (rd.get("assigned_to") or "").strip()
    vtype = _normalize_vehicle_type(
        (session or {}).get("vehicle_reassign_vehicle_type")
        or rd.get("vehicle_type")
        or "in_house"
    )
    is_internal = vtype == "in_house"
    if is_internal:
        _show_fleet_vehicle_list(
            sender,
            request_id,
            assignee_code,
            assignee_label,
            vtype,
            deps,
            mode="reassign",
        )
        return

    staff = _staff_wa_for_assignee_code(deps.db, assignee_code)
    staff_wa = staff[0] if staff else ""
    _complete_vehicle_assignment(
        sender,
        request_id,
        rd,
        ref,
        vtype=vtype,
        assignee_code=assignee_code,
        assignee_label=assignee_label,
        staff_wa=staff_wa,
        reassign=True,
        old_wa=old_wa,
        old_name=old_name,
        deps=deps,
    )


def handle_vehicle_reassign_fleet_pick(
    sender: str, incoming: str, session: dict, deps: VehicleRequestDeps
) -> None:
    if not is_logistics_manager(sender, deps.same_whatsapp):
        deps.clear_session(sender)
        return
    session_rid = (session or {}).get("vehicle_fleet_request_id") or ""
    parsed = _parse_vfleet(incoming, request_id_hint=session_rid)
    fleet_code = ""
    request_id = session_rid
    if parsed:
        request_id, fleet_code = parsed
    else:
        pick = (incoming or "").strip()
        opts = (session or {}).get("vehicle_fleet_options") or {}
        if pick.isdigit() and pick in opts and session_rid:
            fleet_code = str(opts[pick]).strip().lower()
    fleet_map = _fleet_vehicle_map()
    fleet_label = fleet_map.get(fleet_code, "")
    if not fleet_code or not fleet_label:
        deps.send_to(sender, "Please pick a vehicle from the list.")
        return
    assignee_code = (session or {}).get("vehicle_fleet_assignee_code") or ""
    assignee_label = (session or {}).get("vehicle_fleet_assignee_label") or ""
    if not assignee_code or not assignee_label:
        deps.clear_session(sender)
        deps.send_to(sender, "Session expired. Try Manage again.")
        return
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.clear_session(sender)
        deps.send_to(sender, "Vehicle request not found.")
        return
    ref, rd = loaded
    if _request_status(rd) != "ASSIGNED":
        deps.clear_session(sender)
        deps.send_to(sender, "Request cannot be re-assigned now.")
        return
    old_wa = (rd.get("assigned_to_wa") or "").strip()
    old_name = (rd.get("assigned_to") or "").strip()
    vtype = _normalize_vehicle_type(
        (session or {}).get("vehicle_fleet_vehicle_type") or "in_house"
    )
    staff = _staff_wa_for_assignee_code(deps.db, assignee_code)
    staff_wa = staff[0] if staff else ""
    _complete_vehicle_assignment(
        sender,
        request_id,
        rd,
        ref,
        vtype=vtype,
        assignee_code=assignee_code,
        assignee_label=assignee_label,
        staff_wa=staff_wa,
        fleet_vehicle_code=fleet_code,
        fleet_vehicle_label=fleet_label,
        reassign=True,
        old_wa=old_wa,
        old_name=old_name,
        deps=deps,
    )


def _deactivate_assignee_trips(
    db: object,
    assignee_wa: str,
    except_request_id: str,
    deps: VehicleRequestDeps,
) -> None:
    try:
        snaps = db.collection("requests").where("type", "==", "VEHICLE_REQUEST").stream()
    except Exception:
        logger.exception("deactivate trips query failed assignee=%s", assignee_wa)
        return
    for snap in snaps:
        if snap.id == except_request_id:
            continue
        rd = snap.to_dict() or {}
        if (rd.get("assigned_to_wa") or "").strip() != assignee_wa:
            continue
        if not rd.get("is_active_trip"):
            continue
        if rd.get("security_out_at"):
            snap.reference.update({"is_active_trip": False})
            continue
        from firebase_admin import firestore as _firestore

        revert_update: dict = {
            "is_active_trip": False,
            "vehicle_request_status": "ASSIGNED",
        }
        if _request_status(rd) == "STARTED" or rd.get("started_at"):
            revert_update["started_at"] = _firestore.DELETE_FIELD
            revert_update["started_by"] = _firestore.DELETE_FIELD
        snap.reference.update(revert_update)
        if rd.get("assignee_can_start") is False:
            continue
        code = rd.get("assigned_to_code") or ""
        label = rd.get("assigned_to") or ""
        updated = snap.reference.get().to_dict() or rd
        _notify_internal_assignee(
            deps,
            updated,
            assignee_code=str(code),
            assignee_label=str(label),
            request_id=snap.id,
        )


def _find_assignee_startable_request(db: object, assignee_wa: str) -> str | None:
    """Latest ASSIGNED internal trip for assignee (template Quick Reply sends \"Start\")."""
    wa = (assignee_wa or "").strip()
    if not wa:
        return None
    best_id: str | None = None
    best_ts = ""
    try:
        for snap in db.collection("requests").where(
            "type", "==", "VEHICLE_REQUEST"
        ).stream():
            rd = snap.to_dict() or {}
            if (rd.get("assigned_to_wa") or "").strip() != wa:
                continue
            if _request_status(rd) != "ASSIGNED":
                continue
            if rd.get("assignee_can_start") is False:
                continue
            if _normalize_vehicle_type(rd.get("vehicle_type") or "") != "in_house":
                continue
            ts = rd.get("requested_datetime")
            key = str(ts) if ts is not None else snap.id
            if key >= best_ts:
                best_ts = key
                best_id = snap.id
    except Exception:
        logger.exception(
            "assignee startable request lookup failed assignee=%s", assignee_wa
        )
    return best_id


def handle_assignee_gate(
    sender: str, incoming: str, deps: VehicleRequestDeps
) -> bool:
    request_id = _parse_vehicle_action(incoming, "VEHICLE_START_")
    if not request_id and (incoming or "").strip().lower() == "start":
        request_id = _find_assignee_startable_request(deps.db, sender)
    if not request_id:
        return False
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Vehicle request not found.")
        return True
    ref, rd = loaded
    assignee_wa = (rd.get("assigned_to_wa") or "").strip()
    if not assignee_wa or not deps.same_whatsapp(sender, assignee_wa):
        deps.send_to(sender, "Not authorized for this request.")
        return True
    if rd.get("assignee_can_start") is False:
        deps.send_to(sender, "You cannot start this request.")
        return True
    if _request_status(rd) != "ASSIGNED":
        deps.send_to(sender, f"Request is {_request_status(rd).lower()}.")
        return True
    if _normalize_vehicle_type(rd.get("vehicle_type") or "") != "in_house":
        deps.send_to(sender, "Start is only for internal assignments.")
        return True

    try:
        for snap in deps.db.collection("requests").where(
            "type", "==", "VEHICLE_REQUEST"
        ).stream():
            if snap.id == request_id:
                continue
            other = snap.to_dict() or {}
            if (other.get("assigned_to_wa") or "").strip() != assignee_wa:
                continue
            if not other.get("is_active_trip"):
                continue
            if other.get("security_out_at") and not other.get("security_in_at"):
                deps.send_to(
                    sender,
                    "Complete IN on your current trip before starting another.",
                )
                return True
    except Exception:
        logger.exception("assignee active trip check failed assignee=%s", assignee_wa)

    _deactivate_assignee_trips(deps.db, assignee_wa, request_id, deps)
    ref.update({
        "vehicle_request_status": "STARTED",
        "started_at": deps.utcnow(),
        "started_by": sender,
        "is_active_trip": True,
    })
    deps.send_to(
        sender,
        "Trip started.\nSecurity will record your OUT time at the gate.",
    )
    return True
