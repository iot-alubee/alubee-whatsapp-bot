"""Maintenance request flow — WhatsApp Form for shop-floor departments."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Callable

from bot_shared import get_user_record, query_requests_for_employee, wa_from_10
from interakt_api import send_maintenance_flow_form, wa_id_to_phone
from maintenance_data import (
    ISSUE_LABELS,
    MACHINE_LABELS,
    MACHINE_TYPE_LABELS,
    default_machine_type,
    infer_machine_type,
    is_supported_department,
    issue_category_options,
    machine_no_options,
)

logger = logging.getLogger(__name__)

MAINTENANCE_OPEN_STATUSES = frozenset({"PENDING", "ASSIGNED", "IN_PROGRESS"})

UNSUPPORTED_DEPT_MSG = (
    "Maintenance form is available for PDC, Secondary, Fettling, and CNC departments only."
)
SUPERVISOR_ONLY_MSG = "Maintenance form is available for supervisors only."


def _is_supervisor(ud: dict | None) -> bool:
    return bool(ud and ud.get("is_supervisor"))


def show_maintenance_menu_for_user(user_data: dict | None) -> bool:
    """Show Maintenance - Form for supervisors in supported shop-floor departments."""
    if not maintenance_flow_enabled():
        return False
    if not _is_supervisor(user_data):
        return False
    dept = _normalize_dept((user_data or {}).get("department") or "")
    return is_supported_department(dept)


@dataclass
class MaintenanceDeps:
    db: object
    send_to: Callable[[str, str], None]
    session_merge: Callable[..., None]
    session_ref: Callable[[str], object]
    utcnow: Callable
    clear_session: Callable[[str], None]
    go_main_menu: Callable[[str], None]


def maintenance_flow_template_name() -> str:
    return (os.getenv("MAINTENANCE_FLOW_TEMPLATE_NAME") or "").strip()


def maintenance_flow_enabled() -> bool:
    return bool(maintenance_flow_template_name())


def _normalize_dept(dept: str) -> str:
    d = (dept or "").strip().upper()
    if d == "FET":
        return "FETTLING"
    return d


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


def _notify_numbers() -> list[str]:
    raw = (os.getenv("MAINTENANCE_NOTIFY_WHATSAPP_NUMBERS") or "").strip()
    single = (os.getenv("MAINTENANCE_NOTIFY_WHATSAPP_NUMBER") or "").strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if single:
        parts.append(single)
    wa_ids: list[str] = []
    seen: set[str] = set()
    for p in parts:
        wa = wa_from_10(wa_id_to_phone(p)[-10:])
        if wa and wa not in seen:
            seen.add(wa)
            wa_ids.append(wa)
    return wa_ids


def find_open_maintenance_request(employee: str, db: object) -> tuple[str, dict] | None:
    for snap in query_requests_for_employee(db, "MAINTENANCE", employee):
        rd = snap.to_dict() or {}
        status = (rd.get("maintenance_status") or "PENDING").strip().upper()
        if status in MAINTENANCE_OPEN_STATUSES:
            return snap.id, rd
    return None


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


def parse_flow_response(
    response_json: dict | str | None,
    *,
    department: str = "",
    jmd_route: str = "",
) -> dict | None:
    data = _flow_data_dict(response_json)
    if not data:
        return None

    dept = _normalize_dept(department)
    route = (jmd_route or "").strip().upper()

    machine_no = _normalize_id(_flow_pick(data, "machine_no"))
    issue = _normalize_id(_flow_pick(data, "issue_category"))

    if not dept or not is_supported_department(dept):
        return None

    allowed_machines = {m["id"] for m in machine_no_options(dept, route)}
    if not machine_no or machine_no not in allowed_machines:
        return None

    machine_type = _normalize_id(_flow_pick(data, "machine_type"))
    if not machine_type:
        machine_type = infer_machine_type(dept, machine_no) or default_machine_type(dept)
    if not machine_type:
        return None

    allowed_issues = {i["id"] for i in issue_category_options(dept, machine_type)}
    if not issue or issue not in allowed_issues:
        return None

    if not _issue_photo_from_flow_data(data):
        return None

    return {
        "machine_type": machine_type,
        "machine_type_label": MACHINE_TYPE_LABELS.get(machine_type, machine_type),
        "machine_no": machine_no,
        "machine_no_label": MACHINE_LABELS.get(machine_no, machine_no),
        "issue_category": issue,
        "issue_category_label": ISSUE_LABELS.get(issue, issue),
    }


def _notify_team(deps: MaintenanceDeps, rd: dict, request_id: str) -> None:
    recipients = _notify_numbers()
    if not recipients:
        logger.warning("MAINTENANCE_NOTIFY_WHATSAPP_NUMBER(S) not set — skip notify")
        return
    body = (
        "Maintenance request\n\n"
        f"Employee: {rd.get('employee_name') or '—'}\n"
        f"Department: {rd.get('department') or '—'}\n"
        f"Machine type: {rd.get('machine_type_label') or '—'}\n"
        f"Machine no: {rd.get('machine_no_label') or '—'}\n"
        f"Issue: {rd.get('issue_category_label') or '—'}\n"
        f"Request ID: {request_id}"
    )
    for wa in recipients:
        try:
            deps.send_to(wa, body)
            photo_url = (rd.get("issue_photo_url") or "").strip()
            if photo_url:
                from interakt_api import send_image

                send_image(
                    wa_id_to_phone(wa),
                    photo_url,
                    caption="Maintenance issue photo",
                )
        except Exception:
            logger.exception("maintenance notify failed to=%s request_id=%s", wa, request_id)


def _employee_confirmation(rd: dict) -> str:
    return (
        "Your maintenance request has been submitted.\n\n"
        f"Machine type: {rd.get('machine_type_label') or '—'}\n"
        f"Machine no: {rd.get('machine_no_label') or '—'}\n"
        f"Issue: {rd.get('issue_category_label') or '—'}"
    )


def try_start_form(sender: str, deps: MaintenanceDeps) -> None:
    if find_open_maintenance_request(sender, deps.db):
        deps.send_to(
            sender,
            "You already have an open maintenance request.\n"
            "Please wait until it is completed before raising another.",
        )
        return
    if not maintenance_flow_enabled():
        deps.send_to(
            sender,
            "Maintenance form is not configured yet.\nPlease contact admin.",
        )
        return

    exists, ud = get_user_record(sender)
    if not exists or not ud:
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return

    if not _is_supervisor(ud):
        deps.send_to(sender, SUPERVISOR_ONLY_MSG)
        return

    dept = _normalize_dept(ud.get("department") or "")
    if not is_supported_department(dept):
        deps.send_to(sender, UNSUPPORTED_DEPT_MSG)
        return

    name = ud.get("name") or "Employee"
    if send_maintenance_flow_form(
        wa_id_to_phone(sender),
        employee_name=name,
        department=dept,
        jmd_route=(ud.get("jmd_route") or "").strip(),
    ):
        return
    logger.warning("maintenance flow template send failed sender=%s", sender)
    deps.send_to(
        sender,
        "Could not open Maintenance form. Please try again or contact admin.",
    )


def handle_flow_submission(
    sender: str, response_json: dict | str | None, deps: MaintenanceDeps
) -> None:
    exists, ud = get_user_record(sender)
    if not exists or not ud:
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return

    if not _is_supervisor(ud):
        deps.send_to(sender, SUPERVISOR_ONLY_MSG)
        return

    dept = _normalize_dept(ud.get("department") or "")
    route = (ud.get("jmd_route") or "").strip().upper()
    if not is_supported_department(dept):
        deps.send_to(sender, UNSUPPORTED_DEPT_MSG)
        return

    parsed = parse_flow_response(response_json, department=dept, jmd_route=route)
    if not parsed:
        deps.send_to(
            sender,
            "Could not read the Maintenance form. "
            "Ensure machine, issue category, and photo are filled, then submit again.",
        )
        return

    if find_open_maintenance_request(sender, deps.db):
        deps.send_to(
            sender,
            "You already have an open maintenance request.\n"
            "Please wait until it is completed before raising another.",
        )
        return

    ref = deps.db.collection("requests").document()
    request_id = ref.id
    now = deps.utcnow()
    reason = (
        f"{parsed['machine_no_label']} — {parsed['issue_category_label']}"
    )
    payload = {
        "request_id": request_id,
        "requested_datetime": now,
        "employee": sender,
        "employee_id": ud.get("employee_id") or "",
        "employee_name": ud.get("name") or "Employee",
        "department": dept,
        "type": "MAINTENANCE",
        "reason": reason,
        "machine_type": parsed["machine_type"],
        "machine_type_label": parsed["machine_type_label"],
        "machine_no": parsed["machine_no"],
        "machine_no_label": parsed["machine_no_label"],
        "issue_category": parsed["issue_category"],
        "issue_category_label": parsed["issue_category_label"],
        "issue_photo_url": "",
        "issue_photo_path": "",
        "issue_photo_file_name": "",
        "maintenance_status": "PENDING",
        "submission_source": "whatsapp_flow",
        "manager_status": "N/A",
        "jmd_status": "N/A",
        "md_status": "N/A",
        "source": "whatsapp_request",
    }
    ref.set(payload)

    flow_data = _flow_data_dict(response_json)
    photo_raw = _issue_photo_from_flow_data(flow_data)
    from it_flow_media import photo_debug_summary, process_it_issue_photo

    logger.info(
        "Maintenance flow submit request_id=%s photo=%s",
        request_id,
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
            deps.send_to(
                sender,
                "Request saved but issue photo could not be uploaded. "
                "Please contact maintenance with your request ID.",
            )
        ref.set(merge, merge=True)

    _notify_team(deps, payload, request_id)
    deps.send_to(sender, _employee_confirmation(payload))
    logger.info("maintenance request submitted request_id=%s employee=%s", request_id, sender)
