"""IT support request flow (WhatsApp Form + assignment to IT engineers)."""

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
    send_image,
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

IT_OPEN_STATUSES = frozenset({"QUEUED", "ASSIGNED"})
IT_CONFIRM_CLOSE = "IT_CONFIRM_CLOSE"

IT_CLOSE = "IT_CLOSE"
IT_CANCEL = "IT_CANCEL"

IT_STATES = frozenset({IT_CONFIRM_CLOSE})

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


def _employee_assigned_message(engineer_wa: str, deps: ItDeps) -> str:
    name = _engineer_name(engineer_wa, deps.db)
    mobile = wa_id_to_phone(engineer_wa)
    return f"Your request has been assigned to {name} ({mobile})."


def _format_ist(dt) -> str:
    if not dt:
        return ""
    if hasattr(dt, "timestamp"):
        dt = datetime.fromtimestamp(dt.timestamp(), tz=timezone.utc)
    elif isinstance(dt, datetime) and not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_IST).strftime("%d-%m-%Y %I:%M %p")


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


def _engineer_assignment_message(rd: dict) -> str:
    desc = (rd.get("description") or "").strip()
    desc_line = f"Description: {desc}" if desc else "Description: —"
    machine_line = ""
    machine_label = (rd.get("machine_no_label") or "").strip()
    if machine_label:
        machine_line = f"Machine: {machine_label}\n"
    return (
        "You have been assigned the below ticket\n\n"
        f"Name: {rd.get('employee_name') or 'Employee'}\n"
        f"Department: {rd.get('department') or '—'}\n"
        f"Category: {rd.get('it_category_label') or '—'}\n"
        f"{machine_line}"
        f"Issue: {rd.get('issue_type_label') or '—'}\n"
        f"{desc_line}\n"
        f"Priority: {rd.get('priority_label') or '—'}\n"
        f"Requested at: {_format_ist(rd.get('requested_datetime'))}"
    )


def _engineer_assign_template_name() -> str:
    """Template with Image header — used when the user attached an issue photo."""
    return (
        os.getenv("IT_ENGINEER_ASSIGN_TEMPLATE_NAME")
        or os.getenv("IT_TICKET_NOTIFICATION_TEMPLATE_NAME")
        or "it_ticket_notification"
    ).strip()


def _engineer_assign_body_only_template_name() -> str:
    """Template with no header — used when the user did not attach a photo."""
    return (
        os.getenv("IT_ENGINEER_ASSIGN_BODY_TEMPLATE_NAME")
        or os.getenv("IT_TICKET_NOTIFICATION_BODY_TEMPLATE_NAME")
        or "it_ticket_notification_no_image"
    ).strip()


def _user_issue_photo_url(rd: dict) -> str:
    photo = (rd.get("issue_photo_url") or "").strip()
    if photo.lower().startswith("https://"):
        return photo
    status = (rd.get("issue_photo_status") or "").strip().lower()
    if status == "uploaded":
        logger.error(
            "IT issue photo uploaded but issue_photo_url missing request_id=%s",
            rd.get("request_id") or "—",
        )
    return ""


def _engineer_assign_template_language() -> str:
    return (os.getenv("IT_ENGINEER_ASSIGN_TEMPLATE_LANGUAGE_CODE") or "en").strip()


def _engineer_assign_template_body_fields() -> list[str]:
    raw = (
        os.getenv("IT_ENGINEER_ASSIGN_TEMPLATE_BODY_FIELDS")
        or "employee,department,category,machine,issue,description,priority,requested_at"
    ).strip()
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _engineer_assign_template_values(rd: dict) -> dict[str, str]:
    return {
        "employee": (rd.get("employee_name") or "Employee").strip(),
        "department": (rd.get("department") or "—").strip(),
        "category": (rd.get("it_category_label") or "—").strip(),
        "machine": (rd.get("machine_no_label") or "—").strip() or "—",
        "issue": (rd.get("issue_type_label") or "—").strip(),
        "description": (rd.get("description") or "—").strip() or "—",
        "priority": (rd.get("priority_label") or "—").strip(),
        "requested_at": _format_ist(rd.get("requested_datetime")) or "—",
    }


def _engineer_assign_template_body_values(rd: dict) -> list[str]:
    values = _engineer_assign_template_values(rd)
    fields = _engineer_assign_template_body_fields()
    if len(fields) != 8:
        logger.warning(
            "IT_ENGINEER_ASSIGN_TEMPLATE_BODY_FIELDS should list exactly 8 fields; got %s",
            len(fields),
        )
    return [values.get(key, "—")[:1024] for key in fields]


def _send_engineer_assign_template(
    engineer_wa: str,
    rd: dict,
    db: object,
    *,
    request_id: str = "",
) -> bool:
    phone = wa_id_to_phone(engineer_wa)
    engineer_name = _engineer_name(engineer_wa, db)
    body_values = _engineer_assign_template_body_values(rd)
    callback = (request_id or rd.get("request_id") or "").strip()[:512]
    user_photo = _user_issue_photo_url(rd)

    if user_photo:
        template_name = _engineer_assign_template_name()
        if not template_name:
            return False
        try:
            ensure_customer(phone, name=engineer_name)
            send_template_with_image_header(
                phone,
                template_name,
                user_photo,
                language_code=_engineer_assign_template_language(),
                body_values=body_values,
                callback_data=callback,
                ensure_contact=False,
            )
            logger.info(
                "IT engineer assign template sent engineer=%s request_id=%s "
                "template=%s header=user_photo",
                engineer_wa,
                callback,
                template_name,
            )
            return True
        except Exception:
            logger.exception(
                "IT engineer image template failed engineer=%s request_id=%s template=%s",
                engineer_wa,
                callback,
                template_name,
            )
            return False

    template_name = _engineer_assign_body_only_template_name()
    if not template_name:
        logger.error(
            "IT ticket has no photo — set IT_ENGINEER_ASSIGN_BODY_TEMPLATE_NAME "
            "(Utility template with no header)"
        )
        return False
    try:
        ensure_customer(phone, name=engineer_name)
        send_template(
            phone,
            template_name,
            language_code=_engineer_assign_template_language(),
            body_values=body_values,
            callback_data=callback,
            ensure_contact=False,
        )
        logger.info(
            "IT engineer assign template sent engineer=%s request_id=%s "
            "template=%s header=none",
            engineer_wa,
            callback,
            template_name,
        )
        return True
    except Exception:
        logger.exception(
            "IT engineer body template failed engineer=%s request_id=%s template=%s",
            engineer_wa,
            callback,
            template_name,
        )
        return False


def _notify_engineer_assigned(
    engineer_wa: str,
    rd: dict,
    deps: ItDeps,
    *,
    request_id: str = "",
) -> None:
    if _send_engineer_assign_template(
        engineer_wa, rd, deps.db, request_id=request_id
    ):
        return

    ticket_text = _engineer_assignment_message(rd)
    photo_url = (rd.get("issue_photo_url") or "").strip()
    phone = wa_id_to_phone(engineer_wa)
    engineer_name = _engineer_name(engineer_wa, deps.db)

    if photo_url:
        try:
            send_image(
                phone,
                photo_url,
                caption=ticket_text,
                ensure_contact=True,
                contact_name=engineer_name,
            )
            return
        except Exception:
            logger.exception("IT engineer session image failed engineer=%s", engineer_wa)

        try:
            send_image(
                phone,
                photo_url,
                ensure_contact=True,
                contact_name=engineer_name,
            )
            deps.send_to(engineer_wa, ticket_text)
            return
        except Exception:
            logger.exception("IT engineer image-only send failed engineer=%s", engineer_wa)

    deps.send_to(engineer_wa, ticket_text)


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
    snap = deps.db.collection("requests").document(request_id).get()
    rd = dict(snap.to_dict() or rd)
    rd.update({
        "it_status": "ASSIGNED",
        "assigned_engineer": engineer_wa,
        "assigned_engineer_name": engineer_name,
        "assigned_engineer_slot": engineer_slot,
        "request_id": request_id,
    })
    _notify_engineer_assigned(engineer_wa, rd, deps, request_id=request_id)
    employee = (rd.get("employee") or "").strip()
    if employee:
        deps.send_to(employee, _employee_assigned_message(engineer_wa, deps))


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


def handle_flow_submission(sender: str, response_json: dict | str | None, deps: ItDeps) -> None:
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

    ref = deps.db.collection("requests").document()
    request_id = ref.id
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

    photo_raw = _issue_photo_from_flow_data(flow_data)
    from it_flow_media import photo_debug_summary, process_it_issue_photo

    logger.info(
        "IT flow submit request_id=%s flow_keys=%s photo=%s",
        request_id,
        sorted(flow_data.keys()) if flow_data else [],
        photo_debug_summary(photo_raw),
    )
    if photo_raw:
        photo_fields, status_msg = process_it_issue_photo(photo_raw, request_id)
        merge = {"issue_photo_debug": status_msg}
        if photo_fields:
            merge.update(photo_fields)
            payload.update(photo_fields)
        else:
            merge["issue_photo_status"] = "failed"
        ref.set(merge, merge=True)
    else:
        ref.set(
            {
                "issue_photo_status": "missing",
                "issue_photo_debug": "no_photo_in_payload",
            },
            merge=True,
        )

    pick = _pick_available_engineer(deps.db, deps.same_whatsapp)
    if pick:
        slot, engineer_wa = pick
        _assign_request(request_id, payload, slot, engineer_wa, deps)
    else:
        deps.send_to(
            sender,
            "Your IT request has been submitted. "
            "Our engineers are busy — it will be assigned when one is free.",
        )
