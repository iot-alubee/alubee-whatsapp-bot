"""Vehicle request flow — WhatsApp Form submit, logistics manager assign/cancel.

Access vs assignment (do not conflate):
  - is_logistics_requester  → may open the Vehicle Request form (any department)
  - department == LOGISTICS → staff listed when manager assigns Internal vehicles
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
SESSION_WAITING_VEHICLE_MANAGE_ACTION = "WAITING_VEHICLE_MANAGE_ACTION"
SESSION_WAITING_VEHICLE_REASSIGN = "WAITING_VEHICLE_REASSIGN_PICK"

REQUEST_TYPE_LABELS = {
    "delivery": "Delivery",
    "pickup": "Pickup",
}

DESTINATION_CATEGORY_LABELS = {
    "supplier": "Supplier",
    "sub_contractor": "Sub Contractor",
    "customer": "Customer",
    "purchase": "Purchase",
    "transport_office": "Transport Office",
}

DESTINATION_LABELS = {
    "neocol": "Neocol",
    "v_tech": "V-Tech",
    "chellam_transport": "Chellam Transport",
    "ayyappa_gas_shop": "Ayyappa Gas Shop",
    "ayyappa_gas_godown": "Ayyappa Gas Godown",
    "local_shipcot_area": "Local Shipcot Area",
    "local_hosur": "Local Hosur",
    "alloy_tech": "Alloy Tech",
    "arasanatti": "Arasanatti",
    "bagalur_road": "Bagalur Road",
    "seg_mould_inspection": "SEG Mould Inspection",
    "unit_1_to_unit_2": "Unit-1 to Unit-2",
    "rajeshwari_layout": "Rajeshwari Layout",
    "kamal": "Kamal",
    "lakshmi_steels": "Lakshmi Steels",
    "kamaraj_nagar_supplier": "Kamaraj Nagar Supplier",
    "tvs": "TVS",
    "amara_raja": "Amara Raja",
}

DESTINATION_DISTANCE_KM: dict[str, int] = {
    "neocol": 6,
    "v_tech": 4,
    "chellam_transport": 24,
    "ayyappa_gas_shop": 6,
    "ayyappa_gas_godown": 15,
    "local_shipcot_area": 3,
    "local_hosur": 3,
    "alloy_tech": 17,
    "arasanatti": 5,
    "bagalur_road": 10,
    "seg_mould_inspection": 50,
    "unit_1_to_unit_2": 3,
    "rajeshwari_layout": 5,
    "kamal": 2,
    "lakshmi_steels": 6,
    "kamaraj_nagar_supplier": 4,
    "tvs": 20,
    "amara_raja": 420,
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

_VEHICLE_REQUIRED_TIME_SLOTS: frozenset[str] = frozenset({
    "08:30", "09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
    "12:00", "12:30", "13:00", "13:30", "14:00", "14:30", "15:00",
    "15:30", "16:00", "16:30", "17:00", "17:30", "18:00", "18:30",
    "19:00", "19:30", "20:00",
})

EXTERNAL_VENDORS: list[tuple[str, str]] = [
    ("annai_transport", "Annai Transport"),
    ("challa_transport", "Challa Transport"),
    ("sridhar_transport", "Sridhar Transport"),
    ("chella_transport", "Chella Transport"),
]

_EXTERNAL_VENDOR_LABELS: dict[str, str] = dict(EXTERNAL_VENDORS)
_EXTERNAL_VENDOR_CODES = frozenset(_EXTERNAL_VENDOR_LABELS)

_MANUAL_CATEGORIES = frozenset({"purchase", "transport_office"})
_DROPDOWN_CATEGORIES = frozenset({"supplier", "sub_contractor", "customer"})

VALID_DESTINATIONS_BY_CATEGORY: dict[str, frozenset[str]] = {
    "supplier": frozenset({
        "neocol", "v_tech", "chellam_transport", "ayyappa_gas_shop",
        "ayyappa_gas_godown", "local_shipcot_area", "local_hosur", "alloy_tech",
        "arasanatti", "bagalur_road", "seg_mould_inspection", "unit_1_to_unit_2",
    }),
    "sub_contractor": frozenset({
        "rajeshwari_layout", "kamal", "lakshmi_steels", "kamaraj_nagar_supplier",
    }),
    "customer": frozenset({"tvs", "amara_raja"}),
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
    """Active employees in the Logistics department (Internal assign list)."""
    dept = _logistics_department_name()
    staff: list[tuple[str, str]] = []
    try:
        snaps = db.collection("users").where("department", "==", dept).stream()
    except Exception:
        logger.exception("logistics department user query failed dept=%s", dept)
        return staff
    for snap in snaps:
        ud = snap.to_dict() or {}
        emp_id = (ud.get("employee_id") or "").strip()
        name = (ud.get("name") or emp_id or "Staff").strip()
        if not emp_id:
            continue
        code = _normalize_id(emp_id)
        staff.append((code, name))
    staff.sort(key=lambda item: item[1].lower())
    return staff


def _staff_wa_for_assignee_code(
    db: object, assignee_code: str
) -> tuple[str, str] | None:
    """Resolve internal assignee WhatsApp id + name from employee_id code."""
    code = _normalize_id(assignee_code)
    if not code:
        return None
    dept = _logistics_department_name()
    try:
        snaps = db.collection("users").where("department", "==", dept).stream()
    except Exception:
        logger.exception(
            "staff wa lookup failed dept=%s code=%s", dept, assignee_code
        )
        return None
    for snap in snaps:
        ud = snap.to_dict() or {}
        emp_id = _normalize_id(ud.get("employee_id") or "")
        if emp_id == code:
            name = (ud.get("name") or assignee_code).strip()
            return snap.id, name
    return None


def is_vehicle_assign_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_VEHICLE_ASSIGN


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
        or ""
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
        or "assignee_name,requester,from,request_type,category,destination,time"
    ).strip()
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _assignee_notify_template_values(rd: dict, assignee_name: str) -> dict[str, str]:
    return {
        "assignee_name": _sentence_case_name(assignee_name or "—"),
        "requester": _sentence_case_name(rd.get("employee_name") or "—"),
        "from": rd.get("from_unit_label") or "—",
        "request_type": rd.get("request_type_label") or "—",
        "category": rd.get("destination_category_label") or "—",
        "destination": rd.get("destination_label") or "—",
        "time": rd.get("required_at") or "—",
    }


def _assignee_notify_body(rd: dict, assignee_name: str) -> str:
    v = _assignee_notify_template_values(rd, assignee_name)
    first = (assignee_name or "there").strip().split()[0] if assignee_name else "there"
    return (
        f"Hi {first}, new request has been assigned to you. Please refer below.\n\n"
        f"Requester: {v['requester']}\n"
        f"From: {v['from']}\n"
        f"Request Type: {v['request_type']}\n"
        f"Category: {v['category']}\n"
        f"Destination: {v['destination']}\n"
        f"Time: {v['time']}\n\n"
        "Click 'Start' once you are ready!"
    )


def _assignee_notify_template_body_values(rd: dict, assignee_name: str) -> list[str]:
    values = _assignee_notify_template_values(rd, assignee_name)
    fields = _assignee_notify_template_body_fields()
    if len(fields) != 7:
        logger.warning(
            "VEHICLE_ASSIGNEE_NOTIFY_TEMPLATE_BODY_FIELDS should list exactly 7 "
            "fields for Utility template; got %s",
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
    """Notify logistics department staff when an Internal request is assigned."""
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
    phone = wa_id_to_phone(assignee_wa)
    rid = (request_id or "").strip()
    start_id = f"VEHICLE_START_{rid}"[:256]
    can_start = rd.get("assignee_can_start") is not False
    body = _assignee_notify_body(rd, display_name)
    start_buttons = [(start_id, "Start")] if can_start else []

    if deps.has_active_whatsapp_session(assignee_wa):
        try:
            if start_buttons:
                send_reply_buttons(
                    phone,
                    body,
                    start_buttons,
                    callback_data=rid,
                    ensure_contact=True,
                    contact_name=display_name,
                )
            else:
                deps.send_to(assignee_wa, body)
            return
        except Exception:
            logger.exception(
                "vehicle assignee notify with Start failed assignee=%s request_id=%s",
                assignee_wa,
                request_id,
            )

    if template_name:
        try:
            ensure_customer(phone, name=display_name)
            send_template(
                phone,
                template_name,
                language_code=_assignee_notify_template_language(),
                body_values=_assignee_notify_template_body_values(rd, display_name),
                callback_data=rid,
                ensure_contact=False,
            )
            logger.info(
                "vehicle assignee template sent assignee=%s request_id=%s "
                "(add Quick Reply Start button on template in Meta)",
                assignee_wa,
                request_id,
            )
            return
        except Exception:
            logger.exception(
                "vehicle assignee template failed assignee=%s request_id=%s",
                assignee_wa,
                request_id,
            )

    logger.info(
        "skip vehicle assignee notify assignee=%s (set "
        "VEHICLE_ASSIGNEE_NOTIFY_TEMPLATE_NAME or open WhatsApp session)",
        assignee_wa,
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

    time_norm = req_time_raw.replace(".", ":").upper()
    if ":" in time_norm and time_norm not in _VEHICLE_REQUIRED_TIME_SLOTS:
        time_norm = _normalize_id(time_norm).replace("_", ":")
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
    elif category in _DROPDOWN_CATEGORIES:
        allowed = VALID_DESTINATIONS_BY_CATEGORY.get(category, frozenset())
        if destination not in allowed:
            return None
    else:
        return None

    vehicle_type = _normalize_vehicle_type(_flow_pick(data, "vehicle_type"))
    if vehicle_type not in VEHICLE_TYPE_LABELS:
        logger.warning(
            "vehicle request parse failed: vehicle_type=%r raw=%r keys=%s",
            vehicle_type,
            _flow_pick(data, "vehicle_type"),
            list(data.keys()),
        )
        return None

    hire_type = ""
    if vehicle_type == "external_hire":
        hire_type = _normalize_id(_flow_pick(data, "hire_vehicle_type"))
        if hire_type not in HIRE_VEHICLE_TYPE_LABELS:
            logger.warning(
                "vehicle request parse failed: hire_vehicle_type=%r keys=%s",
                hire_type,
                list(data.keys()),
            )
            return None

    load_size = _normalize_id(_flow_pick(data, "load_size"))
    if load_size not in LOAD_SIZE_LABELS:
        logger.warning(
            "vehicle request parse failed: load_size=%r keys=%s",
            load_size,
            list(data.keys()),
        )
        return None

    required_at = _build_required_at(data)
    required_time = _flow_pick(data, "required_time")

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
    sender: str, incoming: str, deps: VehicleRequestDeps
) -> bool:
    if not is_logistics_manager(sender, deps.same_whatsapp):
        return False

    request_id = _parse_vehicle_action(incoming, "VEHICLE_ASSIGN_")
    if request_id:
        return _handle_assign_click(sender, request_id, deps)

    request_id = _parse_vehicle_action(incoming, "VEHICLE_CANCEL_")
    if request_id:
        return _handle_cancel_click(sender, request_id, deps)

    return False


def _handle_assign_click(sender: str, request_id: str, deps: VehicleRequestDeps) -> bool:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Vehicle request not found.")
        return True
    _ref, rd = loaded
    status = _request_status(rd)
    if status != "PENDING":
        deps.send_to(sender, f"Request already {status.lower()}.")
        return True

    options = _assign_options(deps.db, rd.get("vehicle_type") or "")
    if not options:
        deps.send_to(
            sender,
            f"No staff found in {_logistics_department_name()} department.\n"
            "Please update users in Firestore or contact admin.",
        )
        return True

    rows = [
        (f"VASSIGN_{request_id}_{code}"[:256], label)
        for code, label in options
    ]
    list_rows = [{"id": rid, "title": _sentence_case_name(label)[:24]} for rid, label in rows]
    deps.session_merge(
        sender,
        state=SESSION_WAITING_VEHICLE_ASSIGN,
        vehicle_assign_request_id=request_id,
    )
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            "Select vehicle / transport to assign:",
            list_rows,
            button_label="Assign",
            section_title="Assign",
            callback_data=request_id,
        )
    except Exception:
        logger.exception("vehicle assign list failed request_id=%s", request_id)
        deps.clear_session(sender)
        lines = "\n".join(f"• {label}" for _code, label in options)
        deps.send_to(
            sender,
            f"Could not show assign list. Options:\n{lines}\n\nSend Hi and tap Assign again.",
        )
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
    if not parsed:
        deps.send_to(sender, "Please pick a vehicle / transport from the list.")
        return

    request_id, assignee_code = parsed
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

    allowed = _assign_option_map(deps.db, rd.get("vehicle_type") or "")
    assignee_label = allowed.get(assignee_code)
    if not assignee_label:
        deps.send_to(sender, "Invalid selection.")
        return

    staff = _staff_wa_for_assignee_code(deps.db, assignee_code)
    staff_wa = staff[0] if staff else ""
    ref.update({
        "vehicle_request_status": "ASSIGNED",
        "assigned_to": assignee_label,
        "assigned_to_code": assignee_code,
        "assigned_to_wa": staff_wa,
        "assigned_by": sender,
        "assigned_at": deps.utcnow(),
        "assignee_can_start": True,
        "is_active_trip": False,
    })
    updated = ref.get().to_dict() or rd
    _notify_internal_assignee(
        deps,
        updated,
        assignee_code=assignee_code,
        assignee_label=assignee_label,
        request_id=request_id,
    )
    assignee_display = _sentence_case_name(assignee_label)
    employee = (rd.get("employee") or "").strip()
    if employee:
        deps.send_to(
            employee,
            f"Your vehicle request has been assigned to {assignee_display}.",
        )
    deps.send_to(sender, f"Assigned to {assignee_display}.")
    deps.clear_session(sender)


def _fetch_today_vehicle_requests(db: object) -> list[tuple[str, dict]]:
    today = _ist_now().date()
    rows: list[tuple[str, dict]] = []
    try:
        snaps = db.collection("requests").where("type", "==", "VEHICLE_REQUEST").stream()
    except Exception:
        logger.exception("vehicle manage list query failed")
        return rows
    for snap in snaps:
        rd = snap.to_dict() or {}
        status = _request_status(rd)
        if status in ("CANCELLED", "COMPLETED"):
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
        deps.send_to(sender, "No vehicle requests for today.")
        return
    list_rows = [_manage_list_row_fields(rid, rd) for rid, rd in rows[:10]]
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            "Today's vehicle requests:",
            list_rows,
            button_label="Manage",
            section_title="Today",
            callback_data="vehicle-manage",
        )
    except Exception:
        logger.exception("vehicle manage list failed sender=%s", sender)
        lines = "\n".join(f"• {_manage_row_title(rd)}" for _rid, rd in rows[:10])
        deps.send_to(sender, f"Today's vehicle requests:\n{lines}")


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
    sender: str, incoming: str, deps: VehicleRequestDeps
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
    if request_id:
        return _handle_manage_cancel(sender, request_id, deps)

    request_id = _parse_vehicle_action(incoming, "VMREASSIGN_")
    if request_id:
        return _handle_manage_reassign_click(sender, request_id, deps)

    return False


def handle_manager_manage_action(
    sender: str, incoming: str, session: dict, deps: VehicleRequestDeps
) -> None:
    if not is_logistics_manager(sender, deps.same_whatsapp):
        deps.clear_session(sender)
        return
    session_rid = (session or {}).get("vehicle_manage_request_id") or ""
    request_id = _parse_vehicle_action(incoming, "VMCANCEL_")
    if request_id and (not session_rid or request_id == session_rid):
        _handle_manage_cancel(sender, request_id, deps)
        return
    request_id = _parse_vehicle_action(incoming, "VMREASSIGN_")
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

    current_code = _normalize_id(rd.get("assigned_to_code") or "")
    options = [
        (code, label)
        for code, label in _assign_options(deps.db, rd.get("vehicle_type") or "")
        if code != current_code
    ]
    if not options:
        deps.send_to(sender, "No other assignee available.")
        return True

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
    )
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            "Select new assignee:",
            list_rows,
            button_label="Re Assign",
            section_title="Assignee",
            callback_data=request_id,
        )
    except Exception:
        logger.exception("vehicle reassign list failed request_id=%s", request_id)
        deps.clear_session(sender)
        lines = "\n".join(f"• {label}" for _c, label in options)
        deps.send_to(sender, f"Select new assignee:\n{lines}")
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
    if not parsed:
        deps.send_to(sender, "Please pick a new assignee from the list.")
        return
    request_id, assignee_code = parsed
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

    allowed = _assign_option_map(deps.db, rd.get("vehicle_type") or "")
    assignee_label = allowed.get(assignee_code)
    if not assignee_label:
        deps.send_to(sender, "Invalid selection.")
        return

    old_wa = (rd.get("assigned_to_wa") or "").strip()
    old_name = (rd.get("assigned_to") or "").strip()
    staff = _staff_wa_for_assignee_code(deps.db, assignee_code)
    staff_wa = staff[0] if staff else ""

    ref.update({
        "assigned_to": assignee_label,
        "assigned_to_code": assignee_code,
        "assigned_to_wa": staff_wa,
        "assigned_by": sender,
        "assigned_at": deps.utcnow(),
        "assignee_can_start": True,
        "is_active_trip": False,
        "previous_assignee": old_name,
        "previous_assignee_code": old_code,
        "previous_assignee_wa": old_wa,
        "reassigned_at": deps.utcnow(),
    })
    updated = ref.get().to_dict() or rd

    assignee_display = _sentence_case_name(assignee_label)

    if old_wa:
        deps.send_to(
            old_wa,
            f"The request has been re-assigned to {assignee_display}. Thanks.",
        )

    _notify_internal_assignee(
        deps,
        updated,
        assignee_code=assignee_code,
        assignee_label=assignee_label,
        request_id=request_id,
    )
    employee = (rd.get("employee") or "").strip()
    if employee:
        deps.send_to(
            employee,
            f"Your vehicle request has been re-assigned to {assignee_display}.",
        )
    deps.send_to(sender, f"Re-assigned to {assignee_display}.")
    deps.clear_session(sender)


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
