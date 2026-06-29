"""IT support request flow — user form, manager assign, engineer resolve."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from bot_shared import get_user_record, query_requests_by_type, wa_from_10
from interakt_api import (
    ensure_customer,
    send_list_menu,
    send_list_menu_paged,
    send_template,
    send_template_with_image_header,
    wa_id_to_phone,
)

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

IT_ENGINEER_PHONES: tuple[str, ...] = (
    "9994246682",
    "7339221730",
    "9498061569",
)

SESSION_WAITING_IT_MANAGER_ASSIGN = "WAITING_IT_MANAGER_ASSIGN_PICK"
SESSION_WAITING_IT_MANAGER_REASSIGN = "WAITING_IT_MANAGER_REASSIGN_PICK"
SESSION_WAITING_IT_MANAGER_NOTIFY = "WAITING_IT_MANAGER_NOTIFY"

IT_MANAGER_CANNOT_RAISE_MSG = (
    "IT managers cannot raise IT requests.\n"
    "Use IT - List to view and assign tickets."
)
IT_ENGINEER_CANNOT_RAISE_MSG = (
    "IT engineers cannot raise IT requests.\n"
    "Use IT - List to view assigned tickets."
)

CATEGORY_LABELS: dict[str, str] = {
    "printer": "Printer",
    "computer_laptop": "Computer/Laptop",
    "network": "Network",
    "iot": "IoT",
    "alubee_app": "Alubee App",
}

ISSUE_LABELS: dict[str, str] = {
    "printer_connection_error": "Printer Connection Error",
    "not_printing": "Not Printing",
    "printer_not_listed": "Printer Not Listed",
    "os_reinstall": "OS Reinstall",
    "system_hanging_slow": "System Hanging/Slow",
    "excel_office_installation": "Excel/Office Installation",
    "third_party_software": "Third-Party Software Requirements",
    "hardware_issue": "Monitor/Keyboard or Other Hardware Issue",
    "server_not_accessible": "Server Not Accessible",
    "mac_whitelisting": "MAC Whitelisting",
    "internet_speed_issue": "Internet Speed Issue",
    "internet_connection_error": "Internet Connection Error",
    "lcd_led_issue": "LCD/LED Issue",
    "button_issue": "Button Issue",
    "internet_not_connected": "Internet Not Connected",
    "device_freezed": "Device Freezed",
    "reset_issue": "Reset Issue",
    "data_issue": "Data Issue",
    "new_device_installation": "New Device Installation",
    "plan_updation_sheet_issue": "Plan Updation Sheet Issue",
    "data_request": "Data Request",
    "login_issue": "Login Issue",
    "app_not_loading": "App Not Loading",
    "in_out_button_issue": "IN/OUT button issue",
    "other_modification": "Other Modification Requirement",
}

PRIORITY_LABELS = {
    "high": "High",
    "normal": "Normal",
    "low": "Low",
}

MACHINE_LABELS: dict[str, str] = {
    "125t_1": "125T-1",
    "125t_2": "125T-2",
    "125t_3": "125T-3",
    "125t_4": "125T-4",
    "125t_5": "125T-5",
    "125t_6": "125T-6",
    "125t_7": "125T-7",
    "250t_1": "250T-1",
    "350t_1": "350T-1",
    "350t_2": "350T-2",
    "350t_3": "350T-3",
    "350t_4": "350T-4",
    **{f"cnc_{i}": f"CNC-{i}" for i in range(1, 10)},
    **{f"vmc_{i}": f"VMC-{i}" for i in range(1, 9)},
    **{f"fet_{i}": f"FET-{i}" for i in range(1, 16)},
    "fet_17": "FET-17",
    "fet_18": "FET-18",
    "fet_19": "FET-19",
}

IOT_MACHINE_DEPARTMENTS = frozenset({"PDC", "CNC", "FETTLING"})

IOT_MACHINE_IDS_BY_DEPT: dict[str, frozenset[str]] = {
    "PDC": frozenset({
        "125t_1", "125t_2", "125t_3", "125t_4", "125t_5", "125t_6", "125t_7",
        "250t_1", "350t_1", "350t_2", "350t_3", "350t_4",
    }),
    "CNC": frozenset({
        *[f"cnc_{i}" for i in range(1, 10)],
        *[f"vmc_{i}" for i in range(1, 9)],
    }),
    "FETTLING": frozenset({
        *[f"fet_{i}" for i in range(1, 16)],
        "fet_17", "fet_18", "fet_19",
    }),
}

ISSUES_BY_CATEGORY: dict[str, frozenset[str]] = {
    "printer": frozenset({
        "printer_connection_error", "not_printing", "printer_not_listed",
    }),
    "computer_laptop": frozenset({
        "os_reinstall", "system_hanging_slow", "excel_office_installation",
        "third_party_software", "hardware_issue",
    }),
    "network": frozenset({
        "server_not_accessible", "mac_whitelisting", "internet_speed_issue",
        "internet_connection_error",
    }),
    "iot": frozenset({
        "lcd_led_issue", "button_issue", "internet_not_connected", "device_freezed",
        "reset_issue", "data_issue", "new_device_installation",
        "plan_updation_sheet_issue", "data_request",
    }),
    "alubee_app": frozenset({
        "login_issue", "app_not_loading", "in_out_button_issue", "other_modification",
    }),
}


@dataclass
class ItDeps:
    db: object
    send_to: Callable[[str, str], None]
    session_merge: Callable[..., None]
    session_ref: Callable[[str], object]
    utcnow: Callable
    clear_session: Callable[[str], None]
    go_main_menu: Callable[[str], None]
    same_whatsapp: Callable[[str, str], bool]


def it_flow_template_name() -> str:
    return (os.getenv("IT_FLOW_TEMPLATE_NAME") or "").strip()


def it_flow_enabled() -> bool:
    return bool(it_flow_template_name())


def _manager_wa() -> str:
    raw = (
        os.getenv("IT_MANAGER_WHATSAPP_NUMBER")
        or os.getenv("IT_MANAGER_WHATSAPP")
        or ""
    ).strip()
    if not raw:
        return ""
    return wa_from_10(wa_id_to_phone(raw)[-10:])


def is_it_manager(sender: str, same_whatsapp: Callable[[str, str], bool]) -> bool:
    mgr = _manager_wa()
    return bool(mgr and same_whatsapp(sender, mgr))


def is_it_engineer(sender: str) -> bool:
    phone = wa_id_to_phone(sender)[-10:]
    return phone in IT_ENGINEER_PHONES


def show_it_form_for_user(
    user_data: dict | None, wa_id: str, same_whatsapp: Callable[[str, str], bool]
) -> bool:
    if not it_flow_enabled():
        return False
    if is_it_manager(wa_id, same_whatsapp):
        return False
    if is_it_engineer(wa_id):
        return False
    return True


def show_it_list_menu(wa_id: str, same_whatsapp: Callable[[str, str], bool]) -> bool:
    if not it_flow_enabled():
        return False
    return is_it_manager(wa_id, same_whatsapp) or is_it_engineer(wa_id)


def is_it_manager_assign_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_IT_MANAGER_ASSIGN


def is_it_manager_reassign_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_IT_MANAGER_REASSIGN


def is_it_manager_notify_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_IT_MANAGER_NOTIFY


def _engineer_wa_ids() -> list[str]:
    return [wa_from_10(p) for p in IT_ENGINEER_PHONES if wa_from_10(p)]


def _engineer_slot_for_wa(wa_id: str, same_whatsapp: Callable) -> int:
    for slot, phone in enumerate(IT_ENGINEER_PHONES, start=1):
        candidate = wa_from_10(phone)
        if candidate and same_whatsapp(candidate, wa_id):
            return slot
    return 0


def _engineer_name(wa_id: str, db: object) -> str:
    exists, ud = get_user_record(wa_id)
    if exists and ud:
        return (ud.get("name") or "IT Engineer").strip()
    return "IT Engineer"


def _format_ist(dt) -> str:
    if not dt:
        return ""
    if hasattr(dt, "timestamp"):
        dt = datetime.fromtimestamp(dt.timestamp(), tz=timezone.utc)
    elif isinstance(dt, datetime) and not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_IST).strftime("%d-%b-%Y %I:%M %p")


def _request_status(rd: dict) -> str:
    raw = (rd.get("it_status") or "PENDING").strip().upper()
    if raw == "QUEUED":
        return "PENDING"
    return raw


def _load_request(db: object, request_id: str) -> tuple[object, dict] | None:
    rid = (request_id or "").strip()
    if not rid:
        return None
    ref = db.collection("requests").document(rid)
    snap = ref.get()
    if not snap.exists:
        return None
    rd = snap.to_dict() or {}
    if (rd.get("type") or "").strip().upper() != "IT":
        return None
    return ref, rd


def _parse_it_action(incoming: str, prefix: str) -> str | None:
    raw = (incoming or "").strip()
    upper = raw.upper()
    p = prefix.upper()
    if not upper.startswith(p):
        return None
    rid = raw[len(prefix) :].strip()
    return rid or None


def _is_assign_label(raw: str) -> bool:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key in ("assign", "assign_request")


def _is_reassign_label(raw: str) -> bool:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key in ("re_assign", "reassign", "re_assign_request")


def _is_closed_label(raw: str) -> bool:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key in ("closed", "done")


def _is_user_close_label(raw: str) -> bool:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key in (
        "close_ticket",
        "close_ticket_request",
        "close",
        "confirm_close",
    )


def _flow_pick(data: dict, *keys: str) -> str:
    for key in keys:
        val = data.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _resolve_category(raw: str) -> tuple[str, str]:
    key = (raw or "").strip().lower().replace(" ", "_").replace("/", "_")
    aliases = {
        "computer": "computer_laptop",
        "laptop": "computer_laptop",
        "computer_laptop": "computer_laptop",
        "alubee": "alubee_app",
        "app": "alubee_app",
    }
    code = aliases.get(key, key)
    label = CATEGORY_LABELS.get(code, "")
    return code, label


def _resolve_issue(raw: str) -> tuple[str, str]:
    key = (raw or "").strip().lower().replace(" ", "_")
    label = ISSUE_LABELS.get(key, "")
    return key, label


def _resolve_priority(raw: str) -> tuple[str, str]:
    key = (raw or "").strip().lower()
    label = PRIORITY_LABELS.get(key, "")
    return key, label


def _normalize_iot_dept(dept: str) -> str:
    d = (dept or "").strip().upper()
    if d == "FET":
        return "FETTLING"
    if d in IOT_MACHINE_DEPARTMENTS:
        return d
    return ""


def _needs_iot_machine_no(ud: dict | None) -> bool:
    if not ud:
        return False
    dept = _normalize_iot_dept(ud.get("department") or "")
    route = (ud.get("jmd_route") or "").strip().upper()
    return bool(dept) and route == "JMD1" and dept in IOT_MACHINE_IDS_BY_DEPT


def _iot_machine_ids_for_user(ud: dict | None) -> frozenset[str]:
    if not ud:
        return frozenset()
    dept = _normalize_iot_dept(ud.get("department") or "")
    route = (ud.get("jmd_route") or "").strip().upper()
    if not dept or route != "JMD1":
        return frozenset()
    return IOT_MACHINE_IDS_BY_DEPT.get(dept, frozenset())


def _resolve_machine(raw: str) -> tuple[str, str]:
    key = (raw or "").strip().lower()
    label = MACHINE_LABELS.get(key, "")
    return key, label


def _issue_photo_from_flow_data(data: dict) -> object | None:
    from it_flow_media import deep_find_issue_photo

    return deep_find_issue_photo(data)


def _flow_data_dict(response_json: dict | str | None) -> dict:
    if isinstance(response_json, dict):
        return response_json
    if isinstance(response_json, str):
        try:
            parsed = json.loads(response_json)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


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

    category_raw = _flow_pick(data, "it_category", "category")
    issue_raw = _flow_pick(data, "issue_type", "issue")
    priority_raw = _flow_pick(data, "priority")
    description = _flow_pick(data, "description", "issue_description")
    machine_raw = _flow_pick(data, "machine_no")

    category, category_label = _resolve_category(category_raw)
    issue, issue_label = _resolve_issue(issue_raw)
    priority, priority_label = _resolve_priority(priority_raw)
    machine_no, machine_no_label = _resolve_machine(machine_raw)

    if not category or not category_label:
        return None
    if issue not in ISSUES_BY_CATEGORY.get(category, frozenset()):
        return None
    if not priority or not priority_label:
        return None

    out = {
        "it_category": category,
        "it_category_label": category_label,
        "issue_type": issue,
        "issue_type_label": issue_label,
        "description": description,
        "priority": priority,
        "priority_label": priority_label,
    }
    if machine_no:
        out["machine_no"] = machine_no
        out["machine_no_label"] = machine_no_label
    return out


def _user_issue_photo_url(rd: dict) -> str:
    photo = (rd.get("issue_photo_url") or "").strip()
    if photo.lower().startswith("https://"):
        return photo
    return ""


def _split_list_row(row_id: str, line: str) -> dict[str, str]:
    """WhatsApp list row: short title (24) + description (72) for overflow text."""
    parts = [p.strip() for p in (line or "—").split(" - ") if p.strip()]
    title_parts: list[str] = []
    for part in parts:
        candidate = " - ".join(title_parts + [part])
        if len(candidate) <= 24:
            title_parts.append(part)
        else:
            break
    title = " - ".join(title_parts) if title_parts else (line or "—")[:24]
    remainder = parts[len(title_parts):]
    row: dict[str, str] = {"id": row_id, "title": title[:24]}
    if remainder:
        row["description"] = " - ".join(remainder)[:72]
    return row


def _merge_list_row_description(row: dict[str, str], extra: str) -> dict[str, str]:
    extra = (extra or "").strip()
    if not extra:
        return row
    bits = [b for b in (row.get("description") or "", extra) if b.strip()]
    if bits:
        row["description"] = " | ".join(bits)[:72]
    return row


def _manager_list_row_line(rd: dict) -> str:
    """Manager IT - List: Requester - Issue - Pending or assignee name."""
    requester = (rd.get("employee_name") or "—").strip()
    issue = (rd.get("issue_type_label") or "—").strip()
    if _request_status(rd) == "ASSIGNED":
        assignee = (rd.get("assigned_engineer_name") or "Assigned").strip()
    else:
        assignee = "Pending"
    return f"{requester} - {issue} - {assignee}"


def _manager_list_row_extra(rd: dict) -> str:
    bits: list[str] = []
    dept = (rd.get("department") or "").strip()
    if dept:
        bits.append(f"Dept: {dept}")
    machine = (rd.get("machine_no_label") or "").strip()
    if machine:
        bits.append(f"Machine: {machine}")
    category = (rd.get("it_category_label") or "").strip()
    if category:
        bits.append(category)
    priority = (rd.get("priority_label") or "").strip()
    if priority:
        bits.append(f"Priority: {priority}")
    return " | ".join(bits)[:72]


def _engineer_list_row_line(rd: dict) -> str:
    """Engineer IT - List: Requester - Issue - description snippet."""
    requester = (rd.get("employee_name") or "—").strip()
    issue = (rd.get("issue_type_label") or "—").strip()
    if _request_status(rd) == "AWAITING_USER_CLOSE":
        return f"{requester} - {issue} - Awaiting user close"
    desc = " ".join(((rd.get("description") or "").strip() or "—").split())
    return f"{requester} - {issue} - {desc}"


def _engineer_list_row_extra(rd: dict) -> str:
    bits: list[str] = []
    dept = (rd.get("department") or "").strip()
    if dept:
        bits.append(f"Dept: {dept}")
    machine = (rd.get("machine_no_label") or "").strip()
    if machine:
        bits.append(f"Machine: {machine}")
    priority = (rd.get("priority_label") or "").strip()
    if priority:
        bits.append(f"Priority: {priority}")
    requested = _format_ist(rd.get("requested_datetime"))
    if requested:
        bits.append(requested)
    return " | ".join(bits)[:72]


def _manager_list_row_fields(request_id: str, rd: dict) -> dict[str, str]:
    row = _split_list_row(f"ITLIST_{request_id}"[:256], _manager_list_row_line(rd))
    return _merge_list_row_description(row, _manager_list_row_extra(rd))


def _engineer_list_row_fields(request_id: str, rd: dict) -> dict[str, str]:
    row = _split_list_row(f"ITENG_{request_id}"[:256], _engineer_list_row_line(rd))
    return _merge_list_row_description(row, _engineer_list_row_extra(rd))


def _pending_it_manager_notify_request_id(sender: str, deps: ItDeps) -> str:
    snap = deps.session_ref(sender).get()
    if not snap.exists:
        return ""
    data = snap.to_dict() or {}
    if (data.get("state") or "").strip() != SESSION_WAITING_IT_MANAGER_NOTIFY:
        return ""
    return (data.get("it_manager_request_id") or "").strip()


def _normalize_it_callback_request_id(callback_request_id: str) -> str:
    raw = (callback_request_id or "").strip()
    if not raw:
        return ""
    if "-p" in raw:
        return raw.split("-p", 1)[0].strip()
    return raw


def _resolve_it_manager_template_request_id(
    incoming: str,
    sender: str,
    deps: ItDeps,
    *,
    callback_request_id: str = "",
    want: str = "assign",
) -> str:
    raw = (incoming or "").strip()
    if want == "assign" and not _is_assign_label(raw):
        return ""
    if want == "reassign" and not _is_reassign_label(raw):
        return ""
    cb_rid = _normalize_it_callback_request_id(callback_request_id)
    if cb_rid and cb_rid not in ("it-manager-list", "it-engineer-list"):
        return cb_rid
    return _pending_it_manager_notify_request_id(sender, deps)


def _manager_template_name(rd: dict) -> str:
    if _user_issue_photo_url(rd):
        return (
            os.getenv("IT_MANAGER_WITH_PHOTO_TEMPLATE_NAME")
            or "it_manager_wit_photo_v01"
        ).strip()
    return (
        os.getenv("IT_MANAGER_NO_PHOTO_TEMPLATE_NAME")
        or "it_manager_no_photo_v02"
    ).strip()


def _manager_template_language() -> str:
    return (os.getenv("IT_MANAGER_TEMPLATE_LANGUAGE_CODE") or "en").strip()


def _manager_template_body_fields() -> list[str]:
    raw = (
        os.getenv("IT_MANAGER_TEMPLATE_BODY_FIELDS")
        or "name,department,category,machine,issue,description,requested_at"
    ).strip()
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _manager_template_body_values(rd: dict) -> list[str]:
    values = {
        "name": (rd.get("employee_name") or "Employee").strip(),
        "employee": (rd.get("employee_name") or "Employee").strip(),
        "department": (rd.get("department") or "—").strip(),
        "category": (rd.get("it_category_label") or "—").strip(),
        "machine": (rd.get("machine_no_label") or "—").strip() or "—",
        "issue": (rd.get("issue_type_label") or "—").strip(),
        "description": (rd.get("description") or "—").strip() or "—",
        "requested_at": _format_ist(rd.get("requested_datetime")) or "—",
    }
    fields = _manager_template_body_fields()
    return [values.get(key, "—")[:1024] for key in fields]


def _send_manager_ticket_template(
    recipient_wa: str,
    rd: dict,
    request_id: str,
    deps: ItDeps,
    *,
    context: str = "notify",
) -> bool:
    """Manager ticket view — always Meta template; photo header when issue photo exists."""
    template_name = _manager_template_name(rd)
    if not template_name:
        logger.warning("IT manager template not configured request_id=%s", request_id)
        return False

    body_values = _manager_template_body_values(rd)
    photo_url = _user_issue_photo_url(rd)
    phone = wa_id_to_phone(recipient_wa)

    try:
        ensure_customer(phone, name="IT Manager")
        if photo_url:
            send_template_with_image_header(
                phone,
                template_name,
                photo_url,
                language_code=_manager_template_language(),
                body_values=body_values,
                callback_data=request_id[:512],
                ensure_contact=False,
            )
        else:
            send_template(
                phone,
                template_name,
                language_code=_manager_template_language(),
                body_values=body_values,
                callback_data=request_id[:512],
                ensure_contact=False,
            )
        if is_it_manager(recipient_wa, deps.same_whatsapp):
            deps.session_merge(
                recipient_wa,
                state=SESSION_WAITING_IT_MANAGER_NOTIFY,
                it_manager_request_id=request_id,
            )
        logger.info(
            "IT manager template sent context=%s request_id=%s template=%s photo=%s",
            context,
            request_id,
            template_name,
            bool(photo_url),
        )
        return True
    except Exception:
        logger.exception(
            "IT manager template failed context=%s request_id=%s template=%s",
            context,
            request_id,
            template_name,
        )
        return False


def _notify_it_manager(deps: ItDeps, rd: dict, request_id: str) -> None:
    mgr = _manager_wa()
    if not mgr:
        logger.warning("IT_MANAGER_WHATSAPP_NUMBER not set request_id=%s", request_id)
        return
    _send_manager_ticket_template(mgr, rd, request_id, deps, context="new_ticket")


def _engineer_assign_template_name(rd: dict) -> str:
    """Pick engineer template: with-photo header vs body-only (assign and re-assign)."""
    if _user_issue_photo_url(rd):
        return (
            os.getenv("IT_ENGINEER_WITH_PHOTO_TEMPLATE_NAME")
            or os.getenv("IT_ENGINEER_ASSIGN_TEMPLATE_NAME")
            or "it_ticket_with_photo_v01"
        ).strip()
    return (
        os.getenv("IT_ENGINEER_NO_PHOTO_TEMPLATE_NAME")
        or os.getenv("IT_ENGINEER_ASSIGN_BODY_TEMPLATE_NAME")
        or "it_ticket_no_photo_v01"
    ).strip()


def _engineer_assign_template_language() -> str:
    return (os.getenv("IT_ENGINEER_ASSIGN_TEMPLATE_LANGUAGE_CODE") or "en").strip()


def _engineer_assign_template_body_fields() -> list[str]:
    raw = (
        os.getenv("IT_ENGINEER_ASSIGN_TEMPLATE_BODY_FIELDS")
        or "employee,department,category,machine,issue,description,priority,requested_at"
    ).strip()
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _engineer_assign_template_body_values(rd: dict) -> list[str]:
    values = {
        "employee": (rd.get("employee_name") or "Employee").strip(),
        "department": (rd.get("department") or "—").strip(),
        "category": (rd.get("it_category_label") or "—").strip(),
        "machine": (rd.get("machine_no_label") or "—").strip() or "—",
        "issue": (rd.get("issue_type_label") or "—").strip(),
        "description": (rd.get("description") or "—").strip() or "—",
        "priority": (rd.get("priority_label") or "—").strip(),
        "requested_at": _format_ist(rd.get("requested_datetime")) or "—",
    }
    fields = _engineer_assign_template_body_fields()
    return [values.get(key, "—")[:1024] for key in fields]


def _notify_engineer_assigned(
    engineer_wa: str,
    rd: dict,
    deps: ItDeps,
    *,
    request_id: str = "",
    reassign: bool = False,
    context: str = "assign",
) -> bool:
    """Notify engineer — always Meta template; photo header when issue photo exists."""
    template_name = _engineer_assign_template_name(rd)
    if not template_name:
        logger.error("IT engineer assign template not configured request_id=%s", request_id)
        return False

    phone = wa_id_to_phone(engineer_wa)
    engineer_name = _engineer_name(engineer_wa, deps.db)
    body_values = _engineer_assign_template_body_values(rd)
    callback = (request_id or rd.get("request_id") or "").strip()[:512]
    photo_url = _user_issue_photo_url(rd)

    try:
        ensure_customer(phone, name=engineer_name)
        if photo_url:
            send_template_with_image_header(
                phone,
                template_name,
                photo_url,
                language_code=_engineer_assign_template_language(),
                body_values=body_values,
                callback_data=callback,
                ensure_contact=False,
            )
        else:
            send_template(
                phone,
                template_name,
                language_code=_engineer_assign_template_language(),
                body_values=body_values,
                callback_data=callback,
                ensure_contact=False,
            )
        logger.info(
            "IT engineer %s template sent context=%s engineer=%s request_id=%s "
            "template=%s photo=%s",
            "re-assign" if reassign else "assign",
            context,
            engineer_wa,
            callback,
            template_name,
            bool(photo_url),
        )
        return True
    except Exception:
        logger.exception(
            "IT engineer %s template failed context=%s engineer=%s request_id=%s template=%s",
            "re-assign" if reassign else "assign",
            context,
            engineer_wa,
            callback,
            template_name,
        )
        return False


def _user_close_template_name() -> str:
    return (os.getenv("IT_USER_CLOSE_TEMPLATE_NAME") or "it_ticket_close").strip()


def _user_close_template_language() -> str:
    return (os.getenv("IT_USER_CLOSE_TEMPLATE_LANGUAGE_CODE") or "en").strip()


def _user_close_template_body_values(rd: dict, engineer_wa: str, deps: ItDeps) -> list[str]:
    values = {
        "issue": (rd.get("issue_type_label") or "—").strip(),
        "description": (rd.get("description") or "—").strip() or "—",
        "addressed_by": _engineer_name(engineer_wa, deps.db),
    }
    raw = (
        os.getenv("IT_USER_CLOSE_TEMPLATE_BODY_FIELDS")
        or "issue,description,addressed_by"
    ).strip()
    fields = [k.strip().lower() for k in raw.split(",") if k.strip()]
    return [values.get(key, "—")[:1024] for key in fields]


def _notify_user_close_request(
    employee_wa: str, rd: dict, request_id: str, engineer_wa: str, deps: ItDeps
) -> None:
    template_name = _user_close_template_name()
    if not template_name:
        logger.error("IT_USER_CLOSE_TEMPLATE_NAME not set request_id=%s", request_id)
        return
    phone = wa_id_to_phone(employee_wa)
    try:
        ensure_customer(phone, name=(rd.get("employee_name") or "Employee"))
        send_template(
            phone,
            template_name,
            language_code=_user_close_template_language(),
            body_values=_user_close_template_body_values(rd, engineer_wa, deps),
            callback_data=request_id[:512],
            ensure_contact=False,
        )
        logger.info("IT user close template sent request_id=%s", request_id)
    except Exception:
        logger.exception("IT user close template failed request_id=%s", request_id)


def _employee_assigned_message(engineer_wa: str, deps: ItDeps) -> str:
    name = _engineer_name(engineer_wa, deps.db)
    mobile = wa_id_to_phone(engineer_wa)
    return f"Your IT request has been assigned to {name} ({mobile})."


def _complete_it_assignment(
    sender: str,
    request_id: str,
    rd: dict,
    ref: object,
    engineer_wa: str,
    engineer_slot: int,
    deps: ItDeps,
    *,
    reassign: bool = False,
) -> None:
    engineer_name = _engineer_name(engineer_wa, deps.db)
    update = {
        "it_status": "ASSIGNED",
        "assigned_engineer": engineer_wa,
        "assigned_engineer_name": engineer_name,
        "assigned_engineer_slot": engineer_slot,
        "assigned_datetime": deps.utcnow(),
        "assigned_by": sender,
    }
    if reassign:
        update["previous_engineer"] = rd.get("assigned_engineer") or ""
        update["previous_engineer_name"] = rd.get("assigned_engineer_name") or ""
        update["reassigned_at"] = deps.utcnow()
    ref.update(update)
    stored = ref.get().to_dict() or {}
    notify_rd = {**rd, **stored, **update, "request_id": request_id}
    _notify_engineer_assigned(
        engineer_wa,
        notify_rd,
        deps,
        request_id=request_id,
        reassign=reassign,
    )
    employee = (rd.get("employee") or "").strip()
    if employee:
        deps.send_to(employee, _employee_assigned_message(engineer_wa, deps))
    if reassign:
        old_wa = (rd.get("assigned_engineer") or "").strip()
        if old_wa and not deps.same_whatsapp(old_wa, engineer_wa):
            deps.send_to(
                old_wa,
                f"The IT ticket has been re-assigned to {engineer_name}. Thanks.",
            )
        deps.send_to(sender, f"Re-assigned to {engineer_name}.")
    else:
        deps.send_to(sender, f"Assigned to {engineer_name}.")
    deps.clear_session(sender)


def _engineer_options(
    deps: ItDeps, *, exclude_wa: str = ""
) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for slot, phone in enumerate(IT_ENGINEER_PHONES, start=1):
        wa_id = wa_from_10(phone)
        if not wa_id:
            continue
        if exclude_wa and deps.same_whatsapp(wa_id, exclude_wa):
            continue
        out.append((slot, wa_id, _engineer_name(wa_id, deps.db)))
    return out


def _show_engineer_list(
    sender: str,
    request_id: str,
    deps: ItDeps,
    *,
    reassign: bool = False,
    exclude_wa: str = "",
) -> None:
    options = _engineer_options(deps, exclude_wa=exclude_wa)
    if not options:
        deps.send_to(sender, "No engineers available to assign.")
        return
    state = SESSION_WAITING_IT_MANAGER_REASSIGN if reassign else SESSION_WAITING_IT_MANAGER_ASSIGN
    session_key = "it_reassign_request_id" if reassign else "it_assign_request_id"
    list_rows = [
        {"id": f"ITASSIGN_{request_id}_{slot}"[:256], "title": label[:24]}
        for slot, _wa, label in options
    ]
    deps.session_merge(sender, state=state, **{session_key: request_id})
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            "Select engineer:",
            list_rows,
            button_label="Assign",
            section_title="Engineers",
            callback_data=request_id,
        )
    except Exception:
        logger.exception("IT engineer list failed request_id=%s", request_id)
        lines = "\n".join(f"{idx}. {label}" for idx, (_s, _w, label) in enumerate(options, 1))
        deps.send_to(sender, "Select engineer — reply with the number:\n" + lines)


def _send_manager_list_action(sender: str, deps: ItDeps, request_id: str, rd: dict) -> None:
    status = _request_status(rd)
    if status not in ("PENDING", "ASSIGNED"):
        deps.send_to(sender, f"Ticket status: {status.lower()}.")
        return
    if not _send_manager_ticket_template(sender, rd, request_id, deps, context="list"):
        deps.send_to(sender, "Could not load ticket details. Please try IT - List again.")


def _fetch_manager_list(db: object) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    for snap in query_requests_by_type(db, "IT", limit=300):
        rd = snap.to_dict() or {}
        if _request_status(rd) not in ("PENDING", "ASSIGNED"):
            continue
        rows.append((snap.id, rd))
    rows.sort(key=lambda item: item[1].get("requested_datetime") or "", reverse=True)
    return rows[:15]


def try_start_it_list(sender: str, deps: ItDeps) -> None:
    if is_it_manager(sender, deps.same_whatsapp):
        rows = _fetch_manager_list(deps.db)
        if not rows:
            deps.send_to(sender, "No pending or assigned IT requests.")
            return
        list_rows = [
            _manager_list_row_fields(rid, rd) for rid, rd in rows
        ]
        try:
            send_list_menu_paged(
                wa_id_to_phone(sender),
                "IT requests (pending & assigned):",
                list_rows,
                button_label="Open",
                section_title="Tickets",
                callback_data="it-manager-list",
            )
        except Exception:
            lines = "\n".join(f"• {_manager_list_row_line(rd)}" for _rid, rd in rows)
            deps.send_to(sender, "IT requests:\n" + lines)
        return

    if is_it_engineer(sender):
        rows: list[tuple[str, dict]] = []
        for snap in query_requests_by_type(deps.db, "IT", limit=300):
            rd = snap.to_dict() or {}
            if _request_status(rd) not in ("ASSIGNED", "AWAITING_USER_CLOSE"):
                continue
            if not deps.same_whatsapp(rd.get("assigned_engineer"), sender):
                continue
            rows.append((snap.id, rd))
        rows.sort(key=lambda item: item[1].get("requested_datetime") or "", reverse=True)
        rows = rows[:15]
        if not rows:
            deps.send_to(sender, "No active IT tickets.")
            return
        list_rows = [
            _engineer_list_row_fields(rid, rd) for rid, rd in rows
        ]
        try:
            send_list_menu_paged(
                wa_id_to_phone(sender),
                "Your IT tickets (tap to open or request user close):",
                list_rows,
                button_label="Open",
                section_title="Tickets",
                callback_data="it-engineer-list",
            )
        except Exception:
            lines = "\n".join(f"• {_engineer_list_row_line(rd)}" for _rid, rd in rows)
            deps.send_to(sender, "Your assigned tickets:\n" + lines)
        return

    deps.send_to(sender, "Not authorized.")


def _handle_manager_template_assign_click(
    sender: str, request_id: str, deps: ItDeps
) -> bool:
    """Assign from manager template (new ticket or IT - List). Re-assign when already assigned."""
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "IT request not found.")
        return True
    _ref, rd = loaded
    status = _request_status(rd)
    if status == "PENDING":
        _show_engineer_list(sender, request_id, deps, reassign=False)
    elif status == "ASSIGNED":
        old_wa = (rd.get("assigned_engineer") or "").strip()
        _show_engineer_list(sender, request_id, deps, reassign=True, exclude_wa=old_wa)
    else:
        deps.send_to(sender, f"Request is already {status.lower()}.")
    return True


def _handle_manager_assign_click(sender: str, request_id: str, deps: ItDeps) -> bool:
    return _handle_manager_template_assign_click(sender, request_id, deps)


def _handle_manager_reassign_click(sender: str, request_id: str, deps: ItDeps) -> bool:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "IT request not found.")
        return True
    _ref, rd = loaded
    if _request_status(rd) != "ASSIGNED":
        deps.send_to(sender, "Only assigned tickets can be re-assigned.")
        return True
    old_wa = (rd.get("assigned_engineer") or "").strip()
    _show_engineer_list(sender, request_id, deps, reassign=True, exclude_wa=old_wa)
    return True


def handle_it_manager_gate(
    sender: str,
    incoming: str,
    deps: ItDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not is_it_manager(sender, deps.same_whatsapp):
        return False

    request_id = _parse_it_action(incoming, "ITM_ASSIGN_")
    if not request_id:
        request_id = _resolve_it_manager_template_request_id(
            incoming,
            sender,
            deps,
            callback_request_id=callback_request_id,
            want="assign",
        )
    if request_id:
        return _handle_manager_template_assign_click(sender, request_id, deps)

    request_id = _parse_it_action(incoming, "ITMGR_ASSIGN_")
    if request_id:
        return _handle_manager_template_assign_click(sender, request_id, deps)

    request_id = _parse_it_action(incoming, "ITMGR_REASSIGN_")
    if request_id:
        return _handle_manager_reassign_click(sender, request_id, deps)

    if _is_assign_label(incoming):
        deps.send_to(
            sender,
            "Could not identify which ticket to assign.\n"
            "Use IT - List and open the request again.",
        )
        return True

    return False


def handle_it_manager_notify_input(
    sender: str,
    incoming: str,
    session: dict,
    deps: ItDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    """Handle Assign on a manager notify template when session is WAITING_IT_MANAGER_NOTIFY."""
    if not is_it_manager(sender, deps.same_whatsapp):
        return False
    if not is_it_manager_notify_state((session or {}).get("state")):
        return False
    if not _is_assign_label(incoming) and not _is_reassign_label(incoming):
        return False
    request_id = _resolve_it_manager_template_request_id(
        incoming,
        sender,
        deps,
        callback_request_id=callback_request_id,
        want="reassign" if _is_reassign_label(incoming) else "assign",
    )
    if not request_id:
        request_id = (session or {}).get("it_manager_request_id") or ""
    if not request_id:
        deps.send_to(
            sender,
            "Could not identify which ticket to assign.\n"
            "Use IT - List and open the request again.",
        )
        return True
    if _is_reassign_label(incoming):
        return _handle_manager_reassign_click(sender, request_id.strip(), deps)
    return _handle_manager_template_assign_click(sender, request_id.strip(), deps)


def handle_it_manager_list_gate(
    sender: str,
    incoming: str,
    deps: ItDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not is_it_manager(sender, deps.same_whatsapp):
        return False

    key = (incoming or "").strip().upper()
    if key in ("IT_LIST", "IT_LIST_MENU"):
        try_start_it_list(sender, deps)
        return True

    request_id = _parse_it_action(incoming, "ITLIST_")
    if request_id:
        loaded = _load_request(deps.db, request_id)
        if not loaded:
            deps.send_to(sender, "IT request not found.")
            return True
        _ref, rd = loaded
        _send_manager_list_action(sender, deps, request_id, rd)
        return True
    return False


def _parse_itassign(
    incoming: str, *, request_id_hint: str = ""
) -> tuple[str, int] | None:
    raw = (incoming or "").strip()
    if not raw.upper().startswith("ITASSIGN_"):
        return None
    rid_hint = (request_id_hint or "").strip()
    if rid_hint:
        prefix = f"ITASSIGN_{rid_hint}_"
        if raw.startswith(prefix):
            try:
                return rid_hint, int(raw[len(prefix) :].strip())
            except ValueError:
                return None
    rest = raw[9:]
    for slot in (3, 2, 1):
        suffix = f"_{slot}"
        if rest.endswith(suffix):
            rid = rest[: -len(suffix)]
            if rid:
                return rid, slot
    return None


def handle_it_manager_assign_pick(
    sender: str, incoming: str, session: dict, deps: ItDeps
) -> None:
    if not is_it_manager(sender, deps.same_whatsapp):
        deps.clear_session(sender)
        return

    reassign = is_it_manager_reassign_state((session or {}).get("state"))
    session_rid = (
        (session or {}).get("it_reassign_request_id")
        if reassign
        else (session or {}).get("it_assign_request_id")
    ) or ""

    parsed = _parse_itassign(incoming, request_id_hint=session_rid)
    if not parsed:
        deps.send_to(sender, "Invalid selection.")
        return

    request_id, slot = parsed
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.clear_session(sender)
        deps.send_to(sender, "IT request not found.")
        return
    ref, rd = loaded
    want = "ASSIGNED" if reassign else "PENDING"
    if _request_status(rd) != want:
        deps.clear_session(sender)
        deps.send_to(sender, f"Request is already {_request_status(rd).lower()}.")
        return

    engineer_wa = ""
    for s, phone in enumerate(IT_ENGINEER_PHONES, start=1):
        if s == slot:
            engineer_wa = wa_from_10(phone)
            break
    if not engineer_wa:
        deps.send_to(sender, "Invalid engineer.")
        return

    if reassign and deps.same_whatsapp(engineer_wa, rd.get("assigned_engineer") or ""):
        deps.send_to(sender, "Choose a different engineer.")
        return

    _complete_it_assignment(
        sender,
        request_id,
        rd,
        ref,
        engineer_wa,
        slot,
        deps,
        reassign=reassign,
    )


def handle_it_engineer_list_gate(
    sender: str,
    incoming: str,
    deps: ItDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not is_it_engineer(sender):
        return False

    key = (incoming or "").strip().upper()
    if key in ("IT_LIST", "IT_LIST_MENU"):
        try_start_it_list(sender, deps)
        return True

    request_id = _parse_it_action(incoming, "ITENG_")
    if request_id:
        loaded = _load_request(deps.db, request_id)
        if not loaded:
            deps.send_to(sender, "IT request not found.")
            return True
        _ref, rd = loaded
        if not deps.same_whatsapp(rd.get("assigned_engineer"), sender):
            deps.send_to(sender, _engineer_close_denied_message(rd, sender, deps))
            return True
        status = _request_status(rd)
        if status == "AWAITING_USER_CLOSE":
            employee = (rd.get("employee") or "").strip()
            if employee:
                _notify_user_close_request(employee, rd, request_id, sender, deps)
                deps.send_to(
                    sender,
                    "Close request sent to the user again. Thank you.",
                )
            else:
                deps.send_to(sender, "No employee contact on this ticket.")
            return True
        if status != "ASSIGNED":
            deps.send_to(sender, f"Ticket is already {status.lower()}.")
            return True
        if not _notify_engineer_assigned(
            sender, rd, deps, request_id=request_id, context="list"
        ):
            deps.send_to(sender, "Could not load ticket details. Please try IT - List again.")
        return True
    return False


def _engineer_close_denied_message(rd: dict, sender: str, deps: ItDeps) -> str:
    current_name = (rd.get("assigned_engineer_name") or "another engineer").strip()
    prev = (rd.get("previous_engineer") or "").strip()
    status = _request_status(rd)
    if prev and deps.same_whatsapp(prev, sender):
        if status == "CLOSED":
            return (
                f"This ticket has already been re-assigned to {current_name} and is now closed. "
                "You cannot close this ticket."
            )
        if status == "AWAITING_USER_CLOSE":
            return (
                f"This ticket has already been re-assigned to {current_name} and is awaiting "
                "user confirmation. You cannot close this ticket."
            )
        return (
            f"This ticket has already been re-assigned to {current_name}. "
            "You cannot close this ticket."
        )
    if is_it_engineer(sender):
        return (
            f"This ticket is assigned to {current_name}. "
            "You are not authorized to close it."
        )
    return "You are not authorized to close this ticket."


def _find_engineer_closable_request(
    db: object, engineer_wa: str, same_whatsapp: Callable
) -> str:
    """Most recent ASSIGNED ticket for this engineer (Closed without callback)."""
    best_id = ""
    best_ts = None
    for snap in query_requests_by_type(db, "IT", limit=300):
        rd = snap.to_dict() or {}
        if _request_status(rd) != "ASSIGNED":
            continue
        wa = (rd.get("assigned_engineer") or "").strip()
        if not wa or not same_whatsapp(engineer_wa, wa):
            continue
        ts = rd.get("requested_datetime")
        if best_ts is None or (ts and ts > best_ts):
            best_ts = ts
            best_id = snap.id
    return best_id


def _find_user_closable_request(
    db: object, employee_wa: str, same_whatsapp: Callable
) -> str:
    """Most recent AWAITING_USER_CLOSE ticket for this employee (Close Ticket without callback)."""
    best_id = ""
    best_ts = None
    for snap in query_requests_by_type(db, "IT", limit=300):
        rd = snap.to_dict() or {}
        if _request_status(rd) != "AWAITING_USER_CLOSE":
            continue
        wa = (rd.get("employee") or "").strip()
        if not wa or not same_whatsapp(employee_wa, wa):
            continue
        ts = rd.get("engineer_closed_at") or rd.get("requested_datetime")
        if best_ts is None or (ts and ts > best_ts):
            best_ts = ts
            best_id = snap.id
    return best_id


def handle_it_engineer_close_gate(
    sender: str,
    incoming: str,
    deps: ItDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    request_id = _parse_it_action(incoming, "ITCLOSED_")
    if not request_id:
        cb = _normalize_it_callback_request_id(callback_request_id)
        if _is_closed_label(incoming) and cb:
            request_id = cb
    if not request_id and _is_closed_label(incoming):
        request_id = _find_engineer_closable_request(
            deps.db, sender, deps.same_whatsapp
        )
    if not request_id:
        if _is_closed_label(incoming):
            deps.send_to(
                sender,
                "Could not identify the ticket.\nUse IT - List and open the ticket again.",
            )
            return True
        return False

    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "IT request not found.")
        return True
    ref, rd = loaded
    if not deps.same_whatsapp(rd.get("assigned_engineer"), sender):
        if _is_closed_label(incoming) or request_id:
            deps.send_to(sender, _engineer_close_denied_message(rd, sender, deps))
            return True
        return False
    if _request_status(rd) != "ASSIGNED":
        deps.send_to(sender, f"Ticket is already {_request_status(rd).lower()}.")
        return True

    ref.update({
        "it_status": "AWAITING_USER_CLOSE",
        "engineer_closed_at": deps.utcnow(),
        "engineer_closed_by": sender,
    })
    deps.clear_session(sender)
    employee = (rd.get("employee") or "").strip()
    if employee:
        _notify_user_close_request(employee, rd, request_id, sender, deps)
    deps.send_to(sender, "User notified to confirm closure. Thank you.")
    return True


def handle_it_user_close_gate(
    sender: str,
    incoming: str,
    deps: ItDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not _is_user_close_label(incoming) and not _parse_it_action(
        incoming, "ITUSER_CLOSE_"
    ):
        return False

    request_id = _parse_it_action(incoming, "ITUSER_CLOSE_")
    if not request_id:
        cb = _normalize_it_callback_request_id(callback_request_id)
        if _is_user_close_label(incoming) and cb:
            request_id = cb
    if not request_id and _is_user_close_label(incoming):
        request_id = _find_user_closable_request(
            deps.db, sender, deps.same_whatsapp
        )
    if not request_id:
        deps.send_to(
            sender,
            "Could not identify the ticket.\n"
            "Open the IT close message again and tap Close Ticket.",
        )
        return True

    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "IT request not found.")
        return True
    ref, rd = loaded
    if not deps.same_whatsapp(rd.get("employee"), sender):
        deps.send_to(sender, "This IT request does not belong to you.")
        return True
    if _request_status(rd) != "AWAITING_USER_CLOSE":
        deps.send_to(sender, "This ticket is not awaiting your confirmation.")
        return True

    ref.update({
        "it_status": "CLOSED",
        "closed_datetime": deps.utcnow(),
        "closed_by": sender,
    })
    deps.clear_session(sender)
    engineer_wa = (rd.get("assigned_engineer") or "").strip()
    deps.send_to(sender, "Your IT request has been closed. Thank you.")
    if engineer_wa:
        deps.send_to(
            engineer_wa,
            f"IT ticket from {rd.get('employee_name') or 'employee'} has been closed.",
        )
    return True


def try_start_form(sender: str, deps: ItDeps) -> None:
    if is_it_manager(sender, deps.same_whatsapp):
        deps.send_to(sender, IT_MANAGER_CANNOT_RAISE_MSG)
        return
    if is_it_engineer(sender):
        deps.send_to(sender, IT_ENGINEER_CANNOT_RAISE_MSG)
        return

    if not it_flow_enabled():
        deps.send_to(
            sender,
            "IT form is not configured yet. Please contact admin.",
        )
        return

    from interakt_api import send_it_flow_form

    exists, ud = get_user_record(sender)
    name = "Employee"
    department = ""
    jmd_route = ""
    if exists and ud:
        name = ud.get("name") or name
        department = (ud.get("department") or "").strip()
        jmd_route = (ud.get("jmd_route") or "").strip()
    if send_it_flow_form(
        wa_id_to_phone(sender),
        employee_name=name,
        department=department,
        jmd_route=jmd_route,
    ):
        return
    logger.warning("IT flow template send failed sender=%s", sender)
    deps.send_to(sender, "Could not open IT form. Please try again or contact admin.")


def handle_flow_submission(sender: str, response_json: dict | str | None, deps: ItDeps) -> None:
    if is_it_manager(sender, deps.same_whatsapp):
        deps.send_to(sender, IT_MANAGER_CANNOT_RAISE_MSG)
        return
    if is_it_engineer(sender):
        deps.send_to(sender, IT_ENGINEER_CANNOT_RAISE_MSG)
        return

    parsed = parse_flow_response(response_json)
    if not parsed:
        deps.send_to(sender, "Could not read the IT form. Please submit again or contact admin.")
        return

    exists, ud = get_user_record(sender)
    if not exists or not ud:
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return

    flow_data = _flow_data_dict(response_json)
    if (
        parsed.get("it_category") == "iot"
        and _needs_iot_machine_no(ud)
        and parsed.get("machine_no") not in _iot_machine_ids_for_user(ud)
    ):
        deps.send_to(
            sender,
            "Please select a machine number for your IoT request and submit again.",
        )
        return

    photo_raw = _issue_photo_from_flow_data(flow_data)
    from it_flow_media import photo_debug_summary, process_it_issue_photo

    ref = deps.db.collection("requests").document()
    request_id = ref.id
    logger.info(
        "IT flow submit request_id=%s flow_keys=%s photo=%s",
        request_id,
        sorted(flow_data.keys()) if flow_data else [],
        photo_debug_summary(photo_raw),
    )

    photo_fields: dict = {}
    status_msg = "no_photo_in_payload"
    if photo_raw:
        photo_fields, status_msg = process_it_issue_photo(photo_raw, request_id)
        if not photo_fields or not (photo_fields.get("issue_photo_url") or "").strip():
            logger.warning(
                "IT photo upload failed request_id=%s status=%s",
                request_id,
                status_msg,
            )
            deps.send_to(
                sender,
                "Could not upload the issue photo. Please try again.",
            )
            return

    now = deps.utcnow()
    reason = f"{parsed['it_category_label']} — {parsed['issue_type_label']}"
    if parsed.get("machine_no_label"):
        reason = f"{parsed['it_category_label']} — {parsed['machine_no_label']} — {parsed['issue_type_label']}"
    payload = {
        "request_id": request_id,
        "requested_datetime": now,
        "employee": sender,
        "employee_id": ud.get("employee_id") or "",
        "employee_name": ud.get("name") or "Employee",
        "department": ud.get("department") or "",
        "type": "IT",
        "reason": reason,
        "it_category": parsed["it_category"],
        "it_category_label": parsed["it_category_label"],
        "issue_type": parsed["issue_type"],
        "issue_type_label": parsed["issue_type_label"],
        "machine_no": parsed.get("machine_no") or "",
        "machine_no_label": parsed.get("machine_no_label") or "",
        "description": parsed.get("description") or "",
        "priority": parsed["priority"],
        "priority_label": parsed["priority_label"],
        "issue_photo_url": photo_fields.get("issue_photo_url") or "",
        "issue_photo_path": photo_fields.get("issue_photo_path") or "",
        "issue_photo_file_name": photo_fields.get("issue_photo_file_name") or "",
        "issue_photo_status": photo_fields.get("issue_photo_status") or (
            "missing" if not photo_raw else "uploaded"
        ),
        "issue_photo_debug": status_msg,
        "it_status": "PENDING",
        "assigned_engineer": "",
        "assigned_engineer_name": "",
        "assigned_engineer_slot": 0,
        "submission_source": "whatsapp_flow",
        "manager_status": "N/A",
        "jmd_status": "N/A",
        "md_status": "N/A",
        "source": "whatsapp_request",
    }
    ref.set(payload)

    deps.send_to(
        sender,
        "Your IT request has been submitted.\n"
        "The IT manager will assign an engineer shortly.",
    )
    _notify_it_manager(deps, payload, request_id)
