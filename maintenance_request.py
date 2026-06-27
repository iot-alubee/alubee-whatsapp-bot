"""Maintenance request flow — WhatsApp Form for shop-floor departments."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]

from bot_shared import get_user_record, query_requests_for_employee, wa_from_10
from interakt_api import (
    ensure_customer,
    send_list_menu,
    send_maintenance_flow_form,
    send_reply_buttons,
    send_template,
    send_template_with_image_header,
    wa_id_to_phone,
)
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

SESSION_WAITING_MAINTENANCE_ASSIGN = "WAITING_MAINTENANCE_ASSIGN_PICK"
SESSION_WAITING_MAINTENANCE_MANAGER_NOTIFY = "WAITING_MAINTENANCE_MANAGER_NOTIFY"
SESSION_WAITING_MAINTENANCE_MANAGE_ACTION = "WAITING_MAINTENANCE_MANAGE_ACTION"
SESSION_WAITING_MAINTENANCE_REASSIGN = "WAITING_MAINTENANCE_REASSIGN_PICK"

# employee_id, display name — resolved to WhatsApp via Firestore users.
_PDC_ASSIGNEES: tuple[tuple[str, str], ...] = (
    ("adc005", "SIVAKUMAR"),
    ("adc036", "MAHENDHIRAN"),
    ("sri082", "KALAIVANNAN"),
)
_CNC_FET_SEC_ASSIGNEES: tuple[tuple[str, str], ...] = (
    ("adc012", "MURUGESAN"),
    ("adc093", "KANDAN"),
)

UNSUPPORTED_DEPT_MSG = (
    "Maintenance form is available for PDC, Secondary, Fettling, and CNC departments only."
)
SUPERVISOR_ONLY_MSG = "Maintenance form is available for supervisors only."


def _is_supervisor(ud: dict | None) -> bool:
    return bool(ud and ud.get("is_supervisor"))


def is_maintenance_team_user(user_data: dict | None) -> bool:
    """Technicians who receive assigned jobs — cannot raise new requests."""
    emp_id = _normalize_id((user_data or {}).get("employee_id") or "")
    return bool(emp_id and emp_id in _MAINTENANCE_TEAM_CODES)


def show_maintenance_team_list_menu(user_data: dict | None) -> bool:
    if not maintenance_flow_enabled():
        return False
    return is_maintenance_team_user(user_data)


def show_maintenance_menu_for_user(user_data: dict | None) -> bool:
    """Show Maintenance - Form for supervisors in supported shop-floor departments."""
    if not maintenance_flow_enabled():
        return False
    if is_maintenance_team_user(user_data):
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
    same_whatsapp: Callable[[str, str], bool]
    has_active_whatsapp_session: Callable[[str], bool]


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


_MAINTENANCE_TEAM_CODES: frozenset[str] = frozenset(
    _normalize_id(code) for code, _ in (*_PDC_ASSIGNEES, *_CNC_FET_SEC_ASSIGNEES)
)


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


def _ist_now() -> datetime:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _format_ist(dt) -> str:
    if not dt:
        return ""
    if hasattr(dt, "timestamp"):
        dt = datetime.fromtimestamp(dt.timestamp(), tz=timezone.utc)
    elif isinstance(dt, datetime) and not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ist_now().tzinfo).strftime("%d-%m-%Y %I:%M %p")


def _firestore_dt(val) -> datetime | None:
    if val is None:
        return None
    if hasattr(val, "timestamp"):
        dt = datetime.fromtimestamp(val.timestamp(), tz=timezone.utc)
        return dt
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    return None


def _compute_time_taken(requested_dt, completed_dt) -> tuple[str, int]:
    """Human-readable duration and total seconds (requested → closed)."""
    start = _firestore_dt(requested_dt)
    end = _firestore_dt(completed_dt)
    if not start or not end:
        return "", 0
    seconds = max(0, int((end - start).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        display = f"{hours}h {minutes}m"
    elif minutes > 0:
        display = f"{minutes}m"
    else:
        display = f"{secs}s"
    return display, seconds


def _normalize_route(route: str) -> str:
    r = (route or "").strip().upper()
    if r in ("JMD2", "UNIT_II", "UNIT2", "UNIT-2", "UNIT 2"):
        return "JMD2"
    return "JMD1"


def _unit_label(route: str) -> str:
    return "Unit II" if _normalize_route(route) == "JMD2" else "Unit I"


def _manager_wa(route: str) -> str:
    key = _normalize_route(route)
    if key == "JMD2":
        raw = (
            os.getenv("MAINTENANCE_MANAGER_UNIT_II_WHATSAPP_NUMBER")
            or os.getenv("MAINTENANCE_MANAGER_2_WHATSAPP_NUMBER")
            or ""
        ).strip()
    else:
        raw = (
            os.getenv("MAINTENANCE_MANAGER_UNIT_I_WHATSAPP_NUMBER")
            or os.getenv("MAINTENANCE_MANAGER_1_WHATSAPP_NUMBER")
            or ""
        ).strip()
    if not raw:
        return ""
    return wa_from_10(wa_id_to_phone(raw)[-10:])


def _manager_template_name(route: str) -> str:
    shared = (
        os.getenv("MAINTENANCE_MANAGER_NOTIFICATION_TEMPLATE_NAME")
        or os.getenv("MAINTENANCE_MANAGER_TEMPLATE_NAME")
        or ""
    ).strip()
    if shared:
        return shared
    key = _normalize_route(route)
    if key == "JMD2":
        return (
            os.getenv("MAINTENANCE_MANAGER_2_TEMPLATE_NAME")
            or "maintenance_manager_notification_v01"
        ).strip()
    return (
        os.getenv("MAINTENANCE_MANAGER_1_TEMPLATE_NAME")
        or "maintenance_manager_notification_v01"
    ).strip()


def _manager_template_language() -> str:
    return (
        os.getenv("MAINTENANCE_MANAGER_TEMPLATE_LANGUAGE_CODE") or "en"
    ).strip()


def _manager_template_body_fields() -> list[str]:
    raw = (
        os.getenv("MAINTENANCE_MANAGER_TEMPLATE_BODY_FIELDS")
        or "employee,unit,department,machine,issue,requested_at"
    ).strip()
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _manager_template_body_values(rd: dict) -> list[str]:
    values = {
        "employee": (rd.get("employee_name") or "Employee").strip(),
        "unit": _unit_label(rd.get("jmd_route") or ""),
        "department": (rd.get("department") or "—").strip(),
        "machine": (rd.get("machine_no_label") or "—").strip(),
        "issue": (rd.get("issue_category_label") or "—").strip(),
        "requested_at": _format_ist(rd.get("requested_datetime")) or "—",
    }
    fields = _manager_template_body_fields()
    return [values.get(key, "—")[:1024] for key in fields]


def _manager_route_for_sender(
    sender: str, same_whatsapp: Callable[[str, str], bool]
) -> str:
    if same_whatsapp(sender, _manager_wa("JMD1")):
        return "JMD1"
    if same_whatsapp(sender, _manager_wa("JMD2")):
        return "JMD2"
    return ""


def is_maintenance_manager(
    sender: str, same_whatsapp: Callable[[str, str], bool]
) -> bool:
    return bool(_manager_route_for_sender(sender, same_whatsapp))


def show_maintenance_manager_menu(
    wa_id: str, same_whatsapp: Callable[[str, str], bool]
) -> bool:
    return is_maintenance_manager(wa_id, same_whatsapp)


def is_maintenance_assign_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_MAINTENANCE_ASSIGN


def is_maintenance_reassign_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_MAINTENANCE_REASSIGN


def is_maintenance_manage_action_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_MAINTENANCE_MANAGE_ACTION


def _request_status(rd: dict) -> str:
    return (rd.get("maintenance_status") or "PENDING").strip().upper()


def _load_request(db: object, request_id: str) -> tuple[object, dict] | None:
    rid = (request_id or "").strip()
    if not rid:
        return None
    ref = db.collection("requests").document(rid)
    snap = ref.get()
    if not snap.exists:
        return None
    rd = snap.to_dict() or {}
    if (rd.get("type") or "").strip().upper() != "MAINTENANCE":
        return None
    return ref, rd


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


def _assign_options_for_dept(dept: str) -> list[tuple[str, str]]:
    d = _normalize_dept(dept)
    if d == "PDC":
        return list(_PDC_ASSIGNEES)
    if d in ("CNC", "FETTLING", "SECONDARY"):
        return list(_CNC_FET_SEC_ASSIGNEES)
    return []


def _assign_option_map(dept: str) -> dict[str, str]:
    return {code: label for code, label in _assign_options_for_dept(dept)}


def _assignee_wa_for_code(db: object, code: str) -> tuple[str, str] | None:
    norm = _normalize_id(code)
    if not norm:
        return None
    try:
        snaps = db.collection("users").stream()
    except Exception:
        logger.exception("maintenance assignee lookup failed code=%s", code)
        return None
    for snap in snaps:
        ud = snap.to_dict() or {}
        emp_id = _normalize_id(ud.get("employee_id") or "")
        if emp_id == norm:
            name = (ud.get("name") or code).strip()
            return snap.id, name
    return None


def _parse_maintenance_action(incoming: str, prefix: str) -> str | None:
    raw = (incoming or "").strip()
    upper = raw.upper()
    p = prefix.upper()
    if not upper.startswith(p):
        return None
    rid = raw[len(prefix) :].strip()
    return rid or None


def _parse_massign(
    incoming: str, *, request_id_hint: str = "", allowed_codes: frozenset[str]
) -> tuple[str, str] | None:
    raw = (incoming or "").strip()
    if not raw.upper().startswith("MASSIGN_"):
        return None
    rid_hint = (request_id_hint or "").strip()
    if rid_hint:
        prefix = f"MASSIGN_{rid_hint}_"
        if raw.startswith(prefix):
            code = raw[len(prefix) :].strip().lower()
            if code:
                return rid_hint, code
    rest = raw[8:]
    for code in sorted(allowed_codes, key=len, reverse=True):
        suffix = f"_{code}"
        if rest.lower().endswith(suffix.lower()):
            request_id = rest[: -len(suffix)]
            if request_id:
                return request_id, code
    return None


def _is_assign_label(raw: str) -> bool:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key in ("assign", "assign_request", "assign_maintenance")


def _is_reassign_label(raw: str) -> bool:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key in ("re_assign", "reassign", "re_assign_request")


def _set_pending_manager_notify(
    deps: MaintenanceDeps, manager_wa: str, request_id: str
) -> None:
    deps.session_merge(
        manager_wa,
        state=SESSION_WAITING_MAINTENANCE_MANAGER_NOTIFY,
        maintenance_manager_request_id=(request_id or "").strip(),
    )


def _pending_manager_notify_request_id(sender: str, deps: MaintenanceDeps) -> str:
    snap = deps.session_ref(sender).get()
    if not snap.exists:
        return ""
    data = snap.to_dict() or {}
    if data.get("state") != SESSION_WAITING_MAINTENANCE_MANAGER_NOTIFY:
        return ""
    return (data.get("maintenance_manager_request_id") or "").strip()


def _resolve_manager_template_request_id(
    incoming: str,
    sender: str,
    deps: MaintenanceDeps,
    *,
    callback_request_id: str = "",
    want: str,
) -> str | None:
    raw = (incoming or "").strip()
    if want == "assign" and not _is_assign_label(raw):
        return None
    if want == "reassign" and not _is_reassign_label(raw):
        return None
    cb_rid = (callback_request_id or "").strip()
    if cb_rid:
        return cb_rid
    return _pending_manager_notify_request_id(sender, deps) or None


def _pending_manage_action_request_id(sender: str, deps: MaintenanceDeps) -> str:
    snap = deps.session_ref(sender).get()
    if not snap.exists:
        return ""
    data = snap.to_dict() or {}
    if data.get("state") != SESSION_WAITING_MAINTENANCE_MANAGE_ACTION:
        return ""
    return (data.get("maintenance_manage_request_id") or "").strip()


def _resolve_manage_action_request_id(
    incoming: str,
    sender: str,
    deps: MaintenanceDeps,
    *,
    callback_request_id: str = "",
    want: str,
) -> str | None:
    if want != "reassign" or not _is_reassign_label(incoming):
        return None
    cb_rid = (callback_request_id or "").strip()
    if cb_rid:
        return cb_rid
    return _pending_manage_action_request_id(sender, deps) or None


def _notify_maintenance_manager(
    deps: MaintenanceDeps, rd: dict, request_id: str
) -> None:
    route = _normalize_route(rd.get("jmd_route") or "")
    mgr = _manager_wa(route)
    if not mgr:
        logger.warning(
            "maintenance manager WA not set for route=%s request_id=%s",
            route,
            request_id,
        )
        return

    photo_url = _issue_photo_url(rd)
    if not photo_url:
        logger.error(
            "maintenance manager notify skipped — no photo request_id=%s",
            request_id,
        )
        return

    template_name = _manager_template_name(route)
    if not template_name:
        logger.warning("maintenance manager template not configured route=%s", route)
        return

    body_values = _manager_template_body_values(rd)

    try:
        phone = wa_id_to_phone(mgr)
        ensure_customer(phone, name="Maintenance Manager")
        send_template_with_image_header(
            phone,
            template_name,
            photo_url,
            language_code=_manager_template_language(),
            body_values=body_values,
            callback_data=request_id[:512],
            ensure_contact=False,
        )
        _set_pending_manager_notify(deps, mgr, request_id)
        logger.info(
            "maintenance manager image template sent route=%s request_id=%s "
            "template=%s photo=%s",
            route,
            request_id,
            template_name,
            photo_url[:80],
        )
    except Exception:
        logger.exception(
            "maintenance manager template failed request_id=%s template=%s photo=%s",
            request_id,
            template_name,
            photo_url[:80],
        )


def _notify_assignee(
    deps: MaintenanceDeps, rd: dict, request_id: str, assignee_wa: str
) -> None:
    """Notify technician via approved image-header template (issue photo in header)."""
    if not assignee_wa:
        return

    photo_url = _issue_photo_url(rd)
    if not photo_url:
        logger.error(
            "maintenance assignee notify skipped — no photo request_id=%s",
            request_id,
        )
        return

    template_name = _team_notify_template_name()
    if not template_name:
        logger.warning(
            "maintenance team template not configured request_id=%s", request_id
        )
        return

    body_values = _team_notify_template_body_values(rd, request_id)
    phone = wa_id_to_phone(assignee_wa)
    try:
        ensure_customer(phone, name=(rd.get("assigned_to") or "Maintenance"))
        send_template_with_image_header(
            phone,
            template_name,
            photo_url,
            language_code=_team_notify_template_language(),
            body_values=body_values,
            callback_data=request_id[:512],
            ensure_contact=False,
        )
        logger.info(
            "maintenance team image template sent request_id=%s template=%s photo=%s",
            request_id,
            template_name,
            photo_url[:80],
        )
    except Exception:
        logger.exception(
            "maintenance team template failed request_id=%s template=%s photo=%s",
            request_id,
            template_name,
            photo_url[:80],
        )


def _complete_maintenance_assignment(
    sender: str,
    request_id: str,
    rd: dict,
    ref: object,
    *,
    assignee_code: str,
    assignee_label: str,
    assignee_wa: str,
    reassign: bool = False,
    old_wa: str = "",
    old_name: str = "",
    deps: MaintenanceDeps,
) -> None:
    update = {
        "maintenance_status": "ASSIGNED",
        "assigned_to": assignee_label,
        "assigned_to_code": assignee_code,
        "assigned_to_wa": assignee_wa,
        "assigned_by": sender,
        "assigned_at": deps.utcnow(),
    }
    if reassign:
        update["previous_assignee"] = old_name
        update["previous_assignee_code"] = _normalize_id(rd.get("assigned_to_code") or "")
        update["previous_assignee_wa"] = old_wa
        update["reassigned_at"] = deps.utcnow()
    ref.update(update)
    updated = ref.get().to_dict() or rd
    _notify_assignee(deps, updated, request_id, assignee_wa)
    assignee_display = assignee_label.title()
    if reassign and old_wa:
        deps.send_to(
            old_wa,
            f"The maintenance request has been re-assigned to {assignee_display}. Thanks.",
        )
    employee = (rd.get("employee") or "").strip()
    if employee:
        if reassign:
            msg = (
                f"Your maintenance request has been re-assigned to {assignee_display}."
            )
        else:
            msg = f"Your maintenance request has been assigned to {assignee_display}."
        deps.send_to(employee, msg)
    if reassign:
        deps.send_to(sender, f"Re-assigned to {assignee_display}.")
    else:
        deps.send_to(sender, f"Assigned to {assignee_display}.")
    deps.clear_session(sender)


def _show_assignee_list(
    sender: str,
    request_id: str,
    dept: str,
    deps: MaintenanceDeps,
    *,
    mode: str = "assign",
) -> bool:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Maintenance request not found.")
        return True
    _ref, rd = loaded
    want_status = "ASSIGNED" if mode == "reassign" else "PENDING"
    status = _request_status(rd)
    if status != want_status:
        deps.send_to(sender, f"Request already {status.lower()}.")
        return True

    options = _assign_options_for_dept(dept or rd.get("department") or "")
    if not options:
        deps.send_to(sender, "No assignees configured for this department.")
        return True

    state = (
        SESSION_WAITING_MAINTENANCE_REASSIGN
        if mode == "reassign"
        else SESSION_WAITING_MAINTENANCE_ASSIGN
    )
    session_key = (
        "maintenance_reassign_request_id"
        if mode == "reassign"
        else "maintenance_assign_request_id"
    )
    list_rows = [
        {
            "id": f"MASSIGN_{request_id}_{code}"[:256],
            "title": label[:24],
        }
        for code, label in options
    ]
    deps.session_merge(sender, state=state, **{session_key: request_id})
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            "Select technician to assign:",
            list_rows,
            button_label="Assign",
            section_title="Technicians",
            callback_data=request_id,
        )
    except Exception:
        logger.exception("maintenance assign list failed request_id=%s", request_id)
        numbered: dict[str, str] = {}
        lines: list[str] = []
        for idx, (code, label) in enumerate(options, start=1):
            numbered[str(idx)] = code
            lines.append(f"{idx}. {label}")
        deps.session_merge(
            sender,
            state=state,
            **{
                session_key: request_id,
                "maintenance_assign_options": numbered,
            },
        )
        deps.send_to(
            sender,
            "Select technician — reply with the number:\n" + "\n".join(lines),
        )
    return True


def _handle_assign_click(
    sender: str, request_id: str, deps: MaintenanceDeps
) -> bool:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Maintenance request not found.")
        return True
    _ref, rd = loaded
    if _request_status(rd) != "PENDING":
        deps.send_to(sender, f"Request already {_request_status(rd).lower()}.")
        return True
    mgr_route = _manager_route_for_sender(sender, deps.same_whatsapp)
    req_route = _normalize_route(rd.get("jmd_route") or "")
    if mgr_route and mgr_route != req_route:
        deps.send_to(sender, "This request belongs to the other unit.")
        return True
    return _show_assignee_list(
        sender, request_id, rd.get("department") or "", deps, mode="assign"
    )


def _handle_reassign_click(
    sender: str, request_id: str, deps: MaintenanceDeps
) -> bool:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Maintenance request not found.")
        return True
    _ref, rd = loaded
    if _request_status(rd) != "ASSIGNED":
        deps.send_to(sender, "Only assigned requests can be re-assigned.")
        deps.clear_session(sender)
        return True
    mgr_route = _manager_route_for_sender(sender, deps.same_whatsapp)
    req_route = _normalize_route(rd.get("jmd_route") or "")
    if mgr_route and mgr_route != req_route:
        deps.send_to(sender, "This request belongs to the other unit.")
        return True
    return _show_assignee_list(
        sender, request_id, rd.get("department") or "", deps, mode="reassign"
    )


def handle_maintenance_manager_gate(
    sender: str,
    incoming: str,
    deps: MaintenanceDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not is_maintenance_manager(sender, deps.same_whatsapp):
        return False

    request_id = _parse_maintenance_action(incoming, "MMAINT_ASSIGN_")
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
    return False


def _manage_row_title(rd: dict) -> str:
    return (
        f"{rd.get('machine_no_label') or '—'} — "
        f"{rd.get('issue_category_label') or '—'} "
        f"({rd.get('employee_name') or '—'})"
    )


def _fetch_today_assigned(db: object, route: str) -> list[tuple[str, dict]]:
    today = _ist_now().date()
    norm_route = _normalize_route(route)
    rows: list[tuple[str, dict]] = []
    try:
        snaps = db.collection("requests").where("type", "==", "MAINTENANCE").stream()
    except Exception:
        logger.exception("maintenance manage list query failed")
        return rows
    for snap in snaps:
        rd = snap.to_dict() or {}
        if _request_status(rd) != "ASSIGNED":
            continue
        if _normalize_route(rd.get("jmd_route") or "") != norm_route:
            continue
        if not _request_on_ist_day(rd, today):
            continue
        rows.append((snap.id, rd))
    rows.sort(
        key=lambda item: item[1].get("requested_datetime") or "",
        reverse=True,
    )
    return rows


def try_start_manage(sender: str, deps: MaintenanceDeps) -> None:
    if not is_maintenance_manager(sender, deps.same_whatsapp):
        deps.send_to(sender, "Not authorized.")
        return
    route = _manager_route_for_sender(sender, deps.same_whatsapp)
    rows = _fetch_today_assigned(deps.db, route)
    if not rows:
        deps.send_to(
            sender,
            "No assigned maintenance requests for today.\n"
            "New requests will arrive with an Assign button.",
        )
        return
    list_rows = [
        {"id": f"MMANAGE_{rid}"[:256], "title": _manage_row_title(rd)[:24]}
        for rid, rd in rows[:10]
    ]
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            "Assigned maintenance requests for today:",
            list_rows,
            button_label="Manage",
            section_title="Today",
            callback_data="maintenance-manage",
        )
    except Exception:
        logger.exception("maintenance manage list failed sender=%s", sender)
        lines = "\n".join(f"• {_manage_row_title(rd)}" for _rid, rd in rows[:10])
        deps.send_to(sender, f"Assigned maintenance requests for today:\n{lines}")


def _team_list_row_title(rd: dict) -> str:
    return (
        f"{rd.get('machine_no_label') or '—'} — "
        f"{rd.get('issue_category_label') or '—'}"
    )


def _team_request_detail(rd: dict) -> str:
    return (
        f"Machine: {rd.get('machine_no_label') or '—'}\n"
        f"Type: {rd.get('machine_type_label') or '—'}\n"
        f"Issue: {rd.get('issue_category_label') or '—'}\n"
        f"Unit: {_unit_label(rd.get('jmd_route') or '')}\n"
        f"Dept: {rd.get('department') or '—'}\n"
        f"Requested by: {rd.get('employee_name') or '—'}\n"
        f"Requested at: {_format_ist(rd.get('requested_datetime')) or '—'}"
    )


def _fetch_assignee_assigned(
    db: object, assignee_wa: str, same_whatsapp: Callable[[str, str], bool]
) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    try:
        snaps = db.collection("requests").where("type", "==", "MAINTENANCE").stream()
    except Exception:
        logger.exception("maintenance team list query failed")
        return rows
    for snap in snaps:
        rd = snap.to_dict() or {}
        if _request_status(rd) != "ASSIGNED":
            continue
        wa = (rd.get("assigned_to_wa") or "").strip()
        if not wa or not same_whatsapp(assignee_wa, wa):
            continue
        rows.append((snap.id, rd))
    rows.sort(
        key=lambda item: item[1].get("requested_datetime") or "",
        reverse=True,
    )
    return rows[:10]


def try_start_team_list(sender: str, deps: MaintenanceDeps) -> None:
    exists, ud = get_user_record(sender)
    if not exists or not show_maintenance_team_list_menu(ud):
        deps.send_to(sender, "Not authorized.")
        return
    rows = _fetch_assignee_assigned(deps.db, sender, deps.same_whatsapp)
    if not rows:
        deps.send_to(
            sender,
            "No assigned maintenance requests.\n"
            "You will be notified when a job is assigned to you.",
        )
        return
    list_rows = [
        {"id": f"MTEAM_{rid}"[:256], "title": _team_list_row_title(rd)[:24]}
        for rid, rd in rows
    ]
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            "Your assigned maintenance requests:",
            list_rows,
            button_label="Open",
            section_title="Assigned",
            callback_data="maintenance-team-list",
        )
    except Exception:
        logger.exception("maintenance team list failed sender=%s", sender)
        lines = "\n".join(f"• {_team_list_row_title(rd)}" for _rid, rd in rows)
        deps.send_to(sender, f"Your assigned maintenance requests:\n{lines}")


def _send_team_close_action(
    sender: str, deps: MaintenanceDeps, request_id: str, rd: dict
) -> None:
    if not _is_maintenance_assignee(sender, rd, deps.same_whatsapp):
        deps.send_to(sender, "Not authorized for this request.")
        return
    if _request_status(rd) != "ASSIGNED":
        deps.send_to(
            sender,
            f"Request is already {_request_status(rd).lower()}.",
        )
        return
    closed_id = f"MMAINT_CLOSED_{request_id}"[:256]
    try:
        send_reply_buttons(
            wa_id_to_phone(sender),
            _team_request_detail(rd),
            [(closed_id, "Closed")],
            callback_data=request_id,
        )
    except Exception:
        logger.exception("maintenance team close actions failed request_id=%s", request_id)
        deps.send_to(
            sender,
            _team_request_detail(rd) + "\n\nReply Closed when the job is done.",
        )


def handle_team_list_gate(
    sender: str,
    incoming: str,
    deps: MaintenanceDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    exists, ud = get_user_record(sender)
    if not exists or not show_maintenance_team_list_menu(ud):
        return False

    key = (incoming or "").strip().upper()
    if key in ("MAINTENANCE_LIST", "MAINTENANCE_LIST_MENU"):
        try_start_team_list(sender, deps)
        return True

    request_id = _parse_maintenance_action(incoming, "MTEAM_")
    if request_id:
        loaded = _load_request(deps.db, request_id)
        if not loaded:
            deps.send_to(sender, "Maintenance request not found.")
            return True
        _ref, rd = loaded
        _send_team_close_action(sender, deps, request_id, rd)
        return True
    return False


def _send_manage_actions(
    sender: str, deps: MaintenanceDeps, request_id: str, rd: dict
) -> None:
    status = _request_status(rd)
    if status != "ASSIGNED":
        deps.send_to(
            sender,
            f"{_manage_row_title(rd)}\n\nStatus: {status.lower()}.",
        )
        deps.clear_session(sender)
        return
    reassign_id = f"MMAINT_REASSIGN_{request_id}"[:256]
    deps.session_merge(
        sender,
        state=SESSION_WAITING_MAINTENANCE_MANAGE_ACTION,
        maintenance_manage_request_id=request_id,
    )
    try:
        send_reply_buttons(
            wa_id_to_phone(sender),
            _manage_row_title(rd),
            [(reassign_id, "Re Assign")],
            callback_data=request_id,
        )
    except Exception:
        logger.exception("maintenance manage actions failed request_id=%s", request_id)
        deps.send_to(sender, _manage_row_title(rd) + "\n\nReply Re Assign to continue.")


def handle_manager_manage_gate(
    sender: str,
    incoming: str,
    deps: MaintenanceDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not is_maintenance_manager(sender, deps.same_whatsapp):
        return False

    if incoming.strip().upper() in ("MAINTENANCE_MANAGE", "MAINTENANCE_MANAGE_MENU"):
        try_start_manage(sender, deps)
        return True

    request_id = _parse_maintenance_action(incoming, "MMANAGE_")
    if request_id:
        loaded = _load_request(deps.db, request_id)
        if not loaded:
            deps.send_to(sender, "Maintenance request not found.")
            return True
        _ref, rd = loaded
        _send_manage_actions(sender, deps, request_id, rd)
        return True

    request_id = _parse_maintenance_action(incoming, "MMAINT_REASSIGN_")
    if not request_id:
        request_id = _resolve_manage_action_request_id(
            incoming,
            sender,
            deps,
            callback_request_id=callback_request_id,
            want="reassign",
        )
    if request_id:
        return _handle_reassign_click(sender, request_id, deps)
    return False


def handle_manager_manage_action(
    sender: str,
    incoming: str,
    session: dict,
    deps: MaintenanceDeps,
    *,
    callback_request_id: str = "",
) -> None:
    if not is_maintenance_manager(sender, deps.same_whatsapp):
        deps.clear_session(sender)
        return
    session_rid = (session or {}).get("maintenance_manage_request_id") or ""
    request_id = _parse_maintenance_action(incoming, "MMAINT_REASSIGN_")
    if not request_id:
        request_id = _resolve_manage_action_request_id(
            incoming,
            sender,
            deps,
            callback_request_id=callback_request_id,
            want="reassign",
        )
    if request_id and (not session_rid or request_id == session_rid):
        _handle_reassign_click(sender, request_id, deps)
        return
    deps.send_to(sender, "Please use Re Assign.")


def _complete_assign_pick(
    sender: str,
    request_id: str,
    assignee_code: str,
    session: dict,
    deps: MaintenanceDeps,
    *,
    reassign: bool,
) -> None:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.clear_session(sender)
        deps.send_to(sender, "Maintenance request not found.")
        return
    ref, rd = loaded
    want = "ASSIGNED" if reassign else "PENDING"
    if _request_status(rd) != want:
        deps.clear_session(sender)
        deps.send_to(sender, f"Request already {_request_status(rd).lower()}.")
        return

    allowed = _assign_option_map(rd.get("department") or "")
    assignee_label = allowed.get(assignee_code)
    if not assignee_label:
        deps.send_to(sender, "Invalid selection.")
        return

    if reassign and assignee_code == _normalize_id(rd.get("assigned_to_code") or ""):
        deps.send_to(sender, "Choose a different technician.")
        return

    staff = _assignee_wa_for_code(deps.db, assignee_code)
    if not staff:
        deps.send_to(
            sender,
            f"Could not find WhatsApp for {assignee_label}. Check users in Firestore.",
        )
        return
    assignee_wa, assignee_name = staff
    old_wa = (rd.get("assigned_to_wa") or "").strip() if reassign else ""
    old_name = (rd.get("assigned_to") or "").strip() if reassign else ""
    _complete_maintenance_assignment(
        sender,
        request_id,
        rd,
        ref,
        assignee_code=assignee_code,
        assignee_label=assignee_name or assignee_label,
        assignee_wa=assignee_wa,
        reassign=reassign,
        old_wa=old_wa,
        old_name=old_name,
        deps=deps,
    )


def handle_maintenance_assign_pick(
    sender: str, incoming: str, session: dict, deps: MaintenanceDeps
) -> None:
    if not is_maintenance_manager(sender, deps.same_whatsapp):
        deps.clear_session(sender)
        return
    session_rid = (session or {}).get("maintenance_assign_request_id") or ""
    dept_codes = frozenset(_assign_option_map("PDC")) | frozenset(
        _assign_option_map("CNC")
    )
    parsed = _parse_massign(
        incoming, request_id_hint=session_rid, allowed_codes=dept_codes
    )
    assignee_code = ""
    request_id = session_rid
    if parsed:
        request_id, assignee_code = parsed
    else:
        pick = (incoming or "").strip()
        opts = (session or {}).get("maintenance_assign_options") or {}
        if pick.isdigit() and pick in opts and session_rid:
            assignee_code = str(opts[pick]).strip().lower()
    if not assignee_code:
        deps.send_to(sender, "Please pick a technician from the list.")
        return
    _complete_assign_pick(
        sender, request_id, assignee_code, session, deps, reassign=False
    )


def handle_maintenance_reassign_pick(
    sender: str, incoming: str, session: dict, deps: MaintenanceDeps
) -> None:
    if not is_maintenance_manager(sender, deps.same_whatsapp):
        deps.clear_session(sender)
        return
    session_rid = (session or {}).get("maintenance_reassign_request_id") or ""
    dept_codes = frozenset(_assign_option_map("PDC")) | frozenset(
        _assign_option_map("CNC")
    )
    parsed = _parse_massign(
        incoming, request_id_hint=session_rid, allowed_codes=dept_codes
    )
    assignee_code = ""
    request_id = session_rid
    if parsed:
        request_id, assignee_code = parsed
    else:
        pick = (incoming or "").strip()
        opts = (session or {}).get("maintenance_assign_options") or {}
        if pick.isdigit() and pick in opts and session_rid:
            assignee_code = str(opts[pick]).strip().lower()
    if not assignee_code:
        deps.send_to(sender, "Please pick a technician from the list.")
        return
    _complete_assign_pick(
        sender, request_id, assignee_code, session, deps, reassign=True
    )


def _is_closed_label(raw: str) -> bool:
    key = (raw or "").strip().lower().replace(" ", "_")
    return key in ("closed", "close", "completed", "complete", "done")


def _is_maintenance_assignee(sender: str, rd: dict, same_whatsapp: Callable) -> bool:
    assignee_wa = (rd.get("assigned_to_wa") or "").strip()
    return bool(assignee_wa and same_whatsapp(sender, assignee_wa))


def _find_assignee_closable_request(
    db: object, assignee_wa: str, same_whatsapp: Callable[[str, str], bool]
) -> str:
    """Most recent ASSIGNED maintenance request for this technician."""
    best_id = ""
    best_ts = None
    try:
        snaps = db.collection("requests").where("type", "==", "MAINTENANCE").stream()
    except Exception:
        logger.exception("maintenance closable query failed")
        return ""
    for snap in snaps:
        rd = snap.to_dict() or {}
        if _request_status(rd) != "ASSIGNED":
            continue
        wa = (rd.get("assigned_to_wa") or "").strip()
        if not wa or not same_whatsapp(assignee_wa, wa):
            continue
        ts = rd.get("requested_datetime")
        if best_ts is None or (ts and ts > best_ts):
            best_ts = ts
            best_id = snap.id
    return best_id


def handle_maintenance_assignee_gate(
    sender: str,
    incoming: str,
    deps: MaintenanceDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    request_id = _parse_maintenance_action(incoming, "MMAINT_CLOSED_")
    if not request_id:
        cb = (callback_request_id or "").strip()
        if _is_closed_label(incoming) and cb:
            request_id = cb
    if not request_id and _is_closed_label(incoming):
        request_id = _find_assignee_closable_request(
            deps.db, sender, deps.same_whatsapp
        )
    if not request_id:
        return False

    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Maintenance request not found.")
        return True
    ref, rd = loaded
    if not _is_maintenance_assignee(sender, rd, deps.same_whatsapp):
        deps.send_to(sender, "Not authorized for this request.")
        return True
    if _request_status(rd) != "ASSIGNED":
        deps.send_to(sender, f"Request is already {_request_status(rd).lower()}.")
        return True

    completed_at = deps.utcnow()
    time_taken, time_taken_seconds = _compute_time_taken(
        rd.get("requested_datetime"), completed_at
    )

    ref.update({
        "maintenance_status": "COMPLETED",
        "completed_at": completed_at,
        "completed_by": sender,
        "time_taken": time_taken,
        "time_taken_seconds": time_taken_seconds,
    })
    employee = (rd.get("employee") or "").strip()
    if employee:
        msg = "Your maintenance request has been completed."
        if time_taken:
            msg += f"\nTime taken: {time_taken}."
        msg += "\nThank you."
        deps.send_to(employee, msg)
    mgr = _manager_wa(rd.get("jmd_route") or "")
    if mgr:
        mgr_msg = (
            f"Maintenance closed: {rd.get('machine_no_label') or '—'} — "
            f"{rd.get('issue_category_label') or '—'} "
            f"({rd.get('assigned_to') or 'technician'})"
        )
        if time_taken:
            mgr_msg += f"\nTime taken: {time_taken}."
        deps.send_to(mgr, mgr_msg)
    tech_msg = "Maintenance request marked as closed. Thank you."
    if time_taken:
        tech_msg = f"Maintenance request marked as closed.\nTime taken: {time_taken}. Thank you."
    deps.send_to(sender, tech_msg)
    return True


def _team_notify_template_name() -> str:
    return (
        os.getenv("MAINTENANCE_TEAM_NOTIFY_TEMPLATE_NAME")
        or "maintenance_team_notification_v01"
    ).strip()


def _team_notify_template_language() -> str:
    return (os.getenv("MAINTENANCE_TEAM_NOTIFY_TEMPLATE_LANGUAGE_CODE") or "en").strip()


def _team_notify_template_body_fields() -> list[str]:
    raw = (
        os.getenv("MAINTENANCE_TEAM_NOTIFY_TEMPLATE_BODY_FIELDS")
        or "employee,unit,department,machine,issue,requested_at"
    ).strip()
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _team_notify_template_body_values(rd: dict, request_id: str) -> list[str]:
    values = {
        "employee": (rd.get("employee_name") or "Employee").strip(),
        "unit": _unit_label(rd.get("jmd_route") or ""),
        "department": (rd.get("department") or "—").strip(),
        "machine_type": (rd.get("machine_type_label") or "—").strip(),
        "machine": (rd.get("machine_no_label") or "—").strip(),
        "issue": (rd.get("issue_category_label") or "—").strip(),
        "requested_at": _format_ist(rd.get("requested_datetime")) or "—",
        "request_id": (request_id or rd.get("request_id") or "—").strip(),
    }
    fields = _team_notify_template_body_fields()
    return [values.get(key, "—")[:1024] for key in fields]


def _issue_photo_url(rd: dict) -> str:
    photo = (rd.get("issue_photo_url") or "").strip()
    if photo.lower().startswith("https://"):
        return photo
    status = (rd.get("issue_photo_status") or "").strip().lower()
    if status == "uploaded":
        logger.error(
            "Maintenance photo uploaded but issue_photo_url missing request_id=%s",
            rd.get("request_id") or "—",
        )
    return ""


def _employee_confirmation(rd: dict) -> str:
    return (
        "Your maintenance request has been submitted.\n\n"
        f"Machine type: {rd.get('machine_type_label') or '—'}\n"
        f"Machine no: {rd.get('machine_no_label') or '—'}\n"
        f"Issue: {rd.get('issue_category_label') or '—'}"
    )


def try_start_form(sender: str, deps: MaintenanceDeps) -> None:
    if is_maintenance_manager(sender, deps.same_whatsapp):
        try_start_manage(sender, deps)
        return

    exists, ud = get_user_record(sender)
    if exists and is_maintenance_team_user(ud):
        deps.send_to(
            sender,
            "Maintenance team cannot raise new requests.\n"
            "Use Maintenance - List to view and close assigned jobs.",
        )
        return

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

    if is_maintenance_team_user(ud):
        deps.send_to(sender, "Maintenance team cannot raise new requests.")
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

    flow_data = _flow_data_dict(response_json)
    photo_raw = _issue_photo_from_flow_data(flow_data)
    from it_flow_media import photo_debug_summary, process_it_issue_photo

    logger.info(
        "Maintenance flow submit request_id=%s photo=%s",
        request_id,
        photo_debug_summary(photo_raw),
    )
    if not photo_raw:
        deps.send_to(
            sender,
            "Issue photo is required. Please attach a photo and submit again.",
        )
        return

    photo_fields, status_msg = process_it_issue_photo(photo_raw, request_id)
    if not photo_fields or not (photo_fields.get("issue_photo_url") or "").strip():
        logger.warning(
            "Maintenance photo upload failed request_id=%s status=%s",
            request_id,
            status_msg,
        )
        deps.send_to(
            sender,
            "Could not upload the issue photo. Please try again.",
        )
        return

    payload = {
        "request_id": request_id,
        "requested_datetime": now,
        "employee": sender,
        "employee_id": ud.get("employee_id") or "",
        "employee_name": ud.get("name") or "Employee",
        "department": dept,
        "jmd_route": route,
        "type": "MAINTENANCE",
        "reason": reason,
        "machine_type": parsed["machine_type"],
        "machine_type_label": parsed["machine_type_label"],
        "machine_no": parsed["machine_no"],
        "machine_no_label": parsed["machine_no_label"],
        "issue_category": parsed["issue_category"],
        "issue_category_label": parsed["issue_category_label"],
        "issue_photo_url": photo_fields.get("issue_photo_url") or "",
        "issue_photo_path": photo_fields.get("issue_photo_path") or "",
        "issue_photo_file_name": photo_fields.get("issue_photo_file_name") or "",
        "issue_photo_status": photo_fields.get("issue_photo_status") or "uploaded",
        "issue_photo_debug": status_msg,
        "maintenance_status": "PENDING",
        "submission_source": "whatsapp_flow",
        "manager_status": "N/A",
        "jmd_status": "N/A",
        "md_status": "N/A",
        "source": "whatsapp_request",
    }
    ref.set(payload)

    _notify_maintenance_manager(deps, payload, request_id)
    deps.send_to(sender, _employee_confirmation(payload))
    logger.info("maintenance request submitted request_id=%s employee=%s", request_id, sender)
