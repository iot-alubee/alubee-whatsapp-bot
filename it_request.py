"""IT support request flow (WhatsApp Form + assignment to IT engineers)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from bot_shared import get_user_record, query_requests_by_type, query_requests_for_employee, wa_from_10
from interakt_api import send_image, send_reply_buttons, wa_id_to_phone

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

IT_ENGINEER_PHONES: tuple[str, ...] = (
    "9994246682",
    "7339221730",
    "9498061569",
)

IT_OPEN_STATUSES = frozenset({"QUEUED", "ASSIGNED"})
IT_CONFIRM_CLOSE = "IT_CONFIRM_CLOSE"

IT_CLOSE = "IT_CLOSE"
IT_CANCEL = "IT_CANCEL"

IT_STATES = frozenset({IT_CONFIRM_CLOSE})

IT_ALREADY_OPEN_MSG = (
    "Your request is already in progress.\n"
    "Would you like to close or cancel it?"
)

IT_ENGINEER_CANNOT_RAISE_MSG = (
    "IT support engineers cannot raise IT requests through this form."
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


def is_it_state(state: str | None) -> bool:
    return (state or "") in IT_STATES


def is_it_engineer(sender: str) -> bool:
    phone = wa_id_to_phone(sender)[-10:]
    return phone in IT_ENGINEER_PHONES


def _engineer_wa_ids() -> list[str]:
    return [wa_from_10(p) for p in IT_ENGINEER_PHONES if wa_from_10(p)]


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
    return dt.astimezone(_IST).strftime("%d-%m-%Y %I:%M %p")


def find_open_it_request(employee: str, db: object) -> tuple[str, dict] | None:
    for snap in query_requests_for_employee(db, "IT", employee):
        d = snap.to_dict() or {}
        status = (d.get("it_status") or "").strip().upper()
        if status in IT_OPEN_STATUSES:
            return snap.id, d
    return None


def _engineer_assigned_count(db: object, engineer_wa: str, same_whatsapp: Callable) -> int:
    count = 0
    for snap in query_requests_by_type(db, "IT", limit=300):
        d = snap.to_dict() or {}
        if (d.get("it_status") or "").strip().upper() != "ASSIGNED":
            continue
        if same_whatsapp(d.get("assigned_engineer"), engineer_wa):
            count += 1
    return count


def _pick_available_engineer(db: object, same_whatsapp: Callable) -> tuple[int, str] | None:
    for slot, phone in enumerate(IT_ENGINEER_PHONES, start=1):
        wa_id = wa_from_10(phone)
        if wa_id and _engineer_assigned_count(db, wa_id, same_whatsapp) == 0:
            return slot, wa_id
    return None


def _oldest_queued(db: object) -> tuple[str, dict] | None:
    best_id = None
    best_data = None
    best_dt = None
    for snap in query_requests_by_type(db, "IT", limit=300):
        d = snap.to_dict() or {}
        if (d.get("it_status") or "").strip().upper() != "QUEUED":
            continue
        dt = d.get("requested_datetime")
        if best_dt is None or (dt and dt < best_dt):
            best_id = snap.id
            best_data = d
            best_dt = dt
    if best_id and best_data:
        return best_id, best_data
    return None


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


def _issue_photo_from_flow_data(data: dict) -> object | None:
    for key in ("issue_photo", "photo", "issue_photos"):
        val = data.get(key)
        if val:
            return val
    return None


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

    category, category_label = _resolve_category(category_raw)
    issue, issue_label = _resolve_issue(issue_raw)
    priority, priority_label = _resolve_priority(priority_raw)

    if not category or not category_label:
        return None
    if issue not in ISSUES_BY_CATEGORY.get(category, frozenset()):
        return None
    if not priority or not priority_label:
        return None

    return {
        "it_category": category,
        "it_category_label": category_label,
        "issue_type": issue,
        "issue_type_label": issue_label,
        "description": description,
        "priority": priority,
        "priority_label": priority_label,
    }


def _engineer_assignment_message(rd: dict) -> str:
    desc = (rd.get("description") or "").strip()
    desc_line = f"Description: {desc}" if desc else "Description: —"
    return (
        "You have been assigned the below ticket\n\n"
        f"Name: {rd.get('employee_name') or 'Employee'}\n"
        f"Department: {rd.get('department') or '—'}\n"
        f"Category: {rd.get('it_category_label') or '—'}\n"
        f"Issue: {rd.get('issue_type_label') or '—'}\n"
        f"{desc_line}\n"
        f"Priority: {rd.get('priority_label') or '—'}\n"
        f"Requested at: {_format_ist(rd.get('requested_datetime'))}"
    )


def _notify_engineer_assigned(engineer_wa: str, rd: dict, deps: ItDeps) -> None:
    deps.send_to(engineer_wa, _engineer_assignment_message(rd))
    photo_url = (rd.get("issue_photo_url") or "").strip()
    if not photo_url:
        return
    phone = wa_id_to_phone(engineer_wa)
    try:
        send_image(phone, photo_url, caption="Issue photo")
    except Exception:
        logger.exception("IT engineer image send failed engineer=%s", engineer_wa)
        deps.send_to(engineer_wa, f"Issue photo: {photo_url}")


def _assign_request(
    request_id: str,
    rd: dict,
    engineer_slot: int,
    engineer_wa: str,
    deps: ItDeps,
) -> None:
    engineer_name = _engineer_name(engineer_wa, deps.db)
    deps.db.collection("requests").document(request_id).set(
        {
            "it_status": "ASSIGNED",
            "assigned_engineer": engineer_wa,
            "assigned_engineer_name": engineer_name,
            "assigned_engineer_slot": engineer_slot,
            "assigned_datetime": deps.utcnow(),
        },
        merge=True,
    )
    rd = dict(rd)
    rd.update({
        "it_status": "ASSIGNED",
        "assigned_engineer": engineer_wa,
        "assigned_engineer_name": engineer_name,
        "assigned_engineer_slot": engineer_slot,
    })
    _notify_engineer_assigned(engineer_wa, rd, deps)
    employee = (rd.get("employee") or "").strip()
    if employee:
        deps.send_to(
            employee,
            f"Your IT request has been assigned to {engineer_name}.",
        )


def process_it_queue(deps: ItDeps, *, preferred_engineer: str | None = None) -> None:
    queued = _oldest_queued(deps.db)
    if not queued:
        return
    request_id, rd = queued

    pick = None
    if preferred_engineer:
        if _engineer_assigned_count(deps.db, preferred_engineer, deps.same_whatsapp) == 0:
            for slot, phone in enumerate(IT_ENGINEER_PHONES, start=1):
                if deps.same_whatsapp(wa_from_10(phone), preferred_engineer):
                    pick = (slot, preferred_engineer)
                    break
    if not pick:
        pick = _pick_available_engineer(deps.db, deps.same_whatsapp)
    if not pick:
        return

    slot, engineer_wa = pick
    _assign_request(request_id, rd, slot, engineer_wa, deps)
    process_it_queue(deps, preferred_engineer=preferred_engineer)


def try_start_form(sender: str, deps: ItDeps) -> None:
    if is_it_engineer(sender):
        deps.send_to(sender, IT_ENGINEER_CANNOT_RAISE_MSG)
        return

    open_req = find_open_it_request(sender, deps.db)
    if open_req:
        request_id, _rd = open_req
        deps.session_merge(
            sender,
            state=IT_CONFIRM_CLOSE,
            it_request_id=request_id,
        )
        try:
            send_reply_buttons(
                wa_id_to_phone(sender),
                IT_ALREADY_OPEN_MSG,
                [(IT_CLOSE, "Close"), (IT_CANCEL, "Cancel")],
            )
        except Exception:
            logger.exception("IT close/cancel buttons failed sender=%s", sender)
            deps.send_to(
                sender,
                f"{IT_ALREADY_OPEN_MSG}\n\nReply CLOSE or CANCEL.",
            )
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
    if exists and ud:
        name = ud.get("name") or name
    if send_it_flow_form(wa_id_to_phone(sender), employee_name=name):
        return
    logger.warning("IT flow template send failed sender=%s", sender)
    deps.send_to(sender, "Could not open IT form. Please try again or contact admin.")


def handle(sender: str, incoming: str, session: dict, deps: ItDeps) -> None:
    state = (session or {}).get("state")
    choice = (incoming or "").strip().upper()

    if state != IT_CONFIRM_CLOSE:
        deps.send_to(sender, "Send Hi and choose IT - Form from the menu.")
        return

    request_id = (session or {}).get("it_request_id") or ""
    if not request_id:
        deps.clear_session(sender)
        deps.send_to(sender, "Could not find your IT request. Send Hi to start again.")
        return

    if choice in ("CANCEL", IT_CANCEL):
        _finish_request(sender, request_id, session, deps, status="CANCELLED", user_msg="Your IT request has been cancelled.")
        return

    if choice not in (IT_CLOSE, "CLOSE"):
        deps.send_to(sender, "Reply CLOSE to mark resolved, or CANCEL to cancel the request.")
        return

    _finish_request(sender, request_id, session, deps, status="CLOSED", user_msg="Your IT request has been closed. Thank you.")


def _finish_request(
    sender: str,
    request_id: str,
    session: dict,
    deps: ItDeps,
    *,
    status: str,
    user_msg: str,
) -> None:
    snap = deps.db.collection("requests").document(request_id).get()
    if not snap.exists:
        deps.clear_session(sender)
        deps.send_to(sender, "IT request not found. Send Hi to start again.")
        return

    rd = snap.to_dict() or {}
    if not deps.same_whatsapp(rd.get("employee"), sender):
        deps.clear_session(sender)
        deps.send_to(sender, "This IT request does not belong to you.")
        return

    current = (rd.get("it_status") or "").strip().upper()
    if current not in IT_OPEN_STATUSES:
        deps.clear_session(sender)
        deps.send_to(sender, "This IT request is already closed.")
        deps.go_main_menu(sender)
        return

    engineer_wa = (rd.get("assigned_engineer") or "").strip()
    deps.db.collection("requests").document(request_id).set(
        {
            "it_status": status,
            "closed_datetime": deps.utcnow(),
            "closed_by": sender,
        },
        merge=True,
    )
    deps.clear_session(sender)
    deps.send_to(sender, user_msg)
    if engineer_wa:
        action = "closed" if status == "CLOSED" else "cancelled"
        deps.send_to(
            engineer_wa,
            f"IT ticket from {rd.get('employee_name') or 'employee'} has been {action} by the requester.",
        )
        process_it_queue(deps, preferred_engineer=engineer_wa)
    else:
        process_it_queue(deps)
    deps.go_main_menu(sender)


def handle_flow_submission(sender: str, response_json: dict | str | None, deps: ItDeps) -> None:
    if is_it_engineer(sender):
        deps.send_to(sender, IT_ENGINEER_CANNOT_RAISE_MSG)
        return

    parsed = parse_flow_response(response_json)
    if not parsed:
        deps.send_to(sender, "Could not read the IT form. Please submit again or contact admin.")
        return

    if find_open_it_request(sender, deps.db):
        deps.send_to(sender, IT_ALREADY_OPEN_MSG)
        return

    exists, ud = get_user_record(sender)
    if not exists or not ud:
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return

    ref = deps.db.collection("requests").document()
    request_id = ref.id
    now = deps.utcnow()
    payload = {
        "request_id": request_id,
        "requested_datetime": now,
        "employee": sender,
        "employee_id": ud.get("employee_id") or "",
        "employee_name": ud.get("name") or "Employee",
        "department": ud.get("department") or "",
        "type": "IT",
        "reason": f"{parsed['it_category_label']} — {parsed['issue_type_label']}",
        "it_category": parsed["it_category"],
        "it_category_label": parsed["it_category_label"],
        "issue_type": parsed["issue_type"],
        "issue_type_label": parsed["issue_type_label"],
        "description": parsed.get("description") or "",
        "priority": parsed["priority"],
        "priority_label": parsed["priority_label"],
        "issue_photo_url": "",
        "issue_photo_path": "",
        "issue_photo_file_name": "",
        "it_status": "QUEUED",
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

    flow_data = _flow_data_dict(response_json)
    photo_raw = _issue_photo_from_flow_data(flow_data)
    if photo_raw:
        from it_flow_media import process_it_issue_photo

        photo_fields = process_it_issue_photo(photo_raw, request_id)
        if photo_fields:
            ref.set(photo_fields, merge=True)
            payload.update(photo_fields)

    pick = _pick_available_engineer(deps.db, deps.same_whatsapp)
    if pick:
        slot, engineer_wa = pick
        _assign_request(request_id, payload, slot, engineer_wa, deps)
        deps.send_to(
            sender,
            f"Your IT request has been submitted and assigned to "
            f"{_engineer_name(engineer_wa, deps.db)}.",
        )
    else:
        deps.send_to(
            sender,
            "Our IT support engineers are currently attending other requests. "
            "Your request has been queued and will be assigned as soon as an engineer is free.",
        )
