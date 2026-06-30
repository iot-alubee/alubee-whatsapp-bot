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

from bot_shared import (
    get_user_record,
    normalize_callback_request_id,
    request_type_for_id,
    wa_from_10,
    wa_from_env,
)
from interakt_api import (
    ensure_customer,
    send_list_menu,
    send_list_menu_paged,
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
    ("sri079", "MANIKANDAN C"),
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


def show_maintenance_list_menu(
    wa_id: str,
    same_whatsapp: Callable[[str, str], bool],
    user_data: dict | None = None,
) -> bool:
    if not maintenance_flow_enabled():
        return False
    if is_maintenance_manager(wa_id, same_whatsapp):
        return True
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


def _jmd_wa_for_unit(route: str) -> str:
    if _normalize_route(route) == "JMD2":
        return wa_from_env("JMD_II_WHATSAPP_NUMBER")
    return wa_from_env("JMD_I_WHATSAPP_NUMBER", "JMD_WHATSAPP_NUMBER")


def _md_wa_for_notify() -> str:
    return wa_from_env("MD_WHATSAPP_NUMBER")


def _jmd_md_notify_recipients(
    route: str, same_whatsapp: Callable[[str, str], bool]
) -> list[str]:
    out: list[str] = []
    for wa in (_jmd_wa_for_unit(route), _md_wa_for_notify()):
        candidate = (wa or "").strip()
        if not candidate:
            continue
        if any(same_whatsapp(candidate, existing) for existing in out):
            continue
        out.append(candidate)
    return out


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


def is_maintenance_manager_notify_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_MAINTENANCE_MANAGER_NOTIFY


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
    cb_rid = _normalize_maint_callback_request_id(callback_request_id)
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
    cb_rid = _normalize_maint_callback_request_id(callback_request_id)
    if cb_rid:
        return cb_rid
    return _pending_manage_action_request_id(sender, deps) or None


def _normalize_maint_callback_request_id(callback_request_id: str) -> str:
    raw = normalize_callback_request_id(callback_request_id)
    if raw in (
        "maintenance-manage",
        "maintenance-team-list",
        "maintenance-manager-list",
    ):
        return ""
    return raw


def _split_list_row(row_id: str, line: str) -> dict[str, str]:
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
    requester = (rd.get("employee_name") or "—").strip()
    issue = (rd.get("issue_category_label") or "—").strip()
    if _request_status(rd) == "ASSIGNED":
        assignee = (rd.get("assigned_to") or "Assigned").strip()
    else:
        assignee = "Pending"
    return f"{requester} - {issue} - {assignee}"


def _manager_list_row_extra(rd: dict) -> str:
    bits: list[str] = []
    machine = (rd.get("machine_no_label") or "").strip()
    if machine:
        bits.append(f"Machine: {machine}")
    dept = (rd.get("department") or "").strip()
    if dept:
        bits.append(f"Dept: {dept}")
    requested = _format_ist(rd.get("requested_datetime"))
    if requested:
        bits.append(requested)
    return " | ".join(bits)[:72]


def _manager_list_row_fields(request_id: str, rd: dict) -> dict[str, str]:
    row = _split_list_row(f"MMANAGE_{request_id}"[:256], _manager_list_row_line(rd))
    return _merge_list_row_description(row, _manager_list_row_extra(rd))


def _team_list_row_line(rd: dict) -> str:
    machine = (rd.get("machine_no_label") or "—").strip()
    issue = (rd.get("issue_category_label") or "—").strip()
    if _request_status(rd) == "AWAITING_USER_CLOSE":
        return f"{machine} - {issue} - Awaiting user close"
    return f"{machine} - {issue}"


def _team_list_row_extra(rd: dict) -> str:
    bits: list[str] = []
    requester = (rd.get("employee_name") or "").strip()
    if requester:
        bits.append(requester)
    dept = (rd.get("department") or "").strip()
    if dept:
        bits.append(f"Dept: {dept}")
    requested = _format_ist(rd.get("requested_datetime"))
    if requested:
        bits.append(requested)
    return " | ".join(bits)[:72]


def _team_list_row_fields(request_id: str, rd: dict) -> dict[str, str]:
    row = _split_list_row(f"MTEAM_{request_id}"[:256], _team_list_row_line(rd))
    return _merge_list_row_description(row, _team_list_row_extra(rd))


def _send_manager_ticket_template_to(
    recipient_wa: str,
    rd: dict,
    request_id: str,
    deps: MaintenanceDeps,
    *,
    context: str = "list",
) -> bool:
    route = _normalize_route(rd.get("jmd_route") or "")
    photo_url = _issue_photo_url(rd)
    if not photo_url:
        logger.error(
            "maintenance manager template skipped — no photo request_id=%s",
            request_id,
        )
        return False

    template_name = _manager_template_name(route)
    if not template_name:
        logger.warning("maintenance manager template not configured route=%s", route)
        return False

    body_values = _manager_template_body_values(rd)
    try:
        phone = wa_id_to_phone(recipient_wa)
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
        if is_maintenance_manager(recipient_wa, deps.same_whatsapp):
            _set_pending_manager_notify(deps, recipient_wa, request_id)
        logger.info(
            "maintenance manager image template sent context=%s request_id=%s "
            "template=%s photo=%s",
            context,
            request_id,
            template_name,
            photo_url[:80],
        )
        return True
    except Exception:
        logger.exception(
            "maintenance manager template failed context=%s request_id=%s template=%s",
            context,
            request_id,
            template_name,
        )
        return False


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
    _send_manager_ticket_template_to(mgr, rd, request_id, deps, context="new_ticket")


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
    _notify_jmd_md_assignment(updated, request_id, deps)
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

    request_id = _parse_maintenance_action(incoming, "MMAINT_REASSIGN_")
    if not request_id:
        request_id = _resolve_manager_template_request_id(
            incoming,
            sender,
            deps,
            callback_request_id=callback_request_id,
            want="reassign",
        )
    if request_id:
        return _handle_reassign_click(sender, request_id, deps)

    if _is_assign_label(incoming) or _is_reassign_label(incoming):
        deps.send_to(
            sender,
            "Could not identify which ticket to manage.\n"
            "Use Maintenance - List and open the request again.",
        )
        return True
    return False


def handle_maintenance_manager_notify_input(
    sender: str,
    incoming: str,
    session: dict,
    deps: MaintenanceDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    """Handle Assign / Re Assign on manager notify template."""
    if not is_maintenance_manager(sender, deps.same_whatsapp):
        return False
    if not is_maintenance_manager_notify_state((session or {}).get("state")):
        return False
    if not _is_assign_label(incoming) and not _is_reassign_label(incoming):
        return False
    request_id = _resolve_manager_template_request_id(
        incoming,
        sender,
        deps,
        callback_request_id=callback_request_id,
        want="reassign" if _is_reassign_label(incoming) else "assign",
    )
    if not request_id:
        request_id = (session or {}).get("maintenance_manager_request_id") or ""
    if not request_id:
        deps.send_to(
            sender,
            "Could not identify which ticket to manage.\n"
            "Use Maintenance - List and open the request again.",
        )
        return True
    if _is_reassign_label(incoming):
        return _handle_reassign_click(sender, request_id.strip(), deps)
    return _handle_assign_click(sender, request_id.strip(), deps)


def _manage_row_title(rd: dict) -> str:
    return (
        f"{rd.get('machine_no_label') or '—'} — "
        f"{rd.get('issue_category_label') or '—'} "
        f"({rd.get('employee_name') or '—'})"
    )


def _fetch_manager_list(db: object, route: str) -> list[tuple[str, dict]]:
    norm_route = _normalize_route(route)
    rows: list[tuple[str, dict]] = []
    try:
        snaps = db.collection("requests").where("type", "==", "MAINTENANCE").stream()
    except Exception:
        logger.exception("maintenance manager list query failed")
        return rows
    for snap in snaps:
        rd = snap.to_dict() or {}
        if _request_status(rd) not in ("PENDING", "ASSIGNED"):
            continue
        if _normalize_route(rd.get("jmd_route") or "") != norm_route:
            continue
        rows.append((snap.id, rd))
    rows.sort(
        key=lambda item: item[1].get("requested_datetime") or "",
        reverse=True,
    )
    return rows[:15]


def try_start_manage(sender: str, deps: MaintenanceDeps) -> None:
    if not is_maintenance_manager(sender, deps.same_whatsapp):
        deps.send_to(sender, "Not authorized.")
        return
    route = _manager_route_for_sender(sender, deps.same_whatsapp)
    rows = _fetch_manager_list(deps.db, route)
    if not rows:
        deps.send_to(
            sender,
            "No pending or assigned maintenance requests for your unit.",
        )
        return
    list_rows = [_manager_list_row_fields(rid, rd) for rid, rd in rows]
    try:
        send_list_menu_paged(
            wa_id_to_phone(sender),
            "Maintenance requests (pending & assigned):",
            list_rows,
            button_label="Open",
            section_title="Tickets",
            callback_data="maintenance-manager-list",
        )
    except Exception:
        logger.exception("maintenance manage list failed sender=%s", sender)
        lines = "\n".join(f"• {_manager_list_row_line(rd)}" for _rid, rd in rows)
        deps.send_to(sender, f"Maintenance requests:\n{lines}")


def try_start_maintenance_list(sender: str, deps: MaintenanceDeps) -> None:
    if is_maintenance_manager(sender, deps.same_whatsapp):
        try_start_manage(sender, deps)
        return
    exists, ud = get_user_record(sender)
    if exists and is_maintenance_team_user(ud):
        try_start_team_list(sender, deps)
        return
    deps.send_to(sender, "Not authorized.")


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
        if _request_status(rd) not in ("ASSIGNED", "AWAITING_USER_CLOSE"):
            continue
        wa = (rd.get("assigned_to_wa") or "").strip()
        if not wa or not same_whatsapp(assignee_wa, wa):
            continue
        rows.append((snap.id, rd))
    rows.sort(
        key=lambda item: item[1].get("requested_datetime") or "",
        reverse=True,
    )
    return rows[:15]


def try_start_team_list(sender: str, deps: MaintenanceDeps) -> None:
    exists, ud = get_user_record(sender)
    if not exists or not show_maintenance_team_list_menu(ud):
        deps.send_to(sender, "Not authorized.")
        return
    rows = _fetch_assignee_assigned(deps.db, sender, deps.same_whatsapp)
    if not rows:
        deps.send_to(
            sender,
            "No active maintenance requests.\n"
            "You will be notified when a job is assigned to you.",
        )
        return
    list_rows = [_team_list_row_fields(rid, rd) for rid, rd in rows]
    try:
        send_list_menu_paged(
            wa_id_to_phone(sender),
            "Your maintenance tickets (tap to open or request user close):",
            list_rows,
            button_label="Open",
            section_title="Tickets",
            callback_data="maintenance-team-list",
        )
    except Exception:
        logger.exception("maintenance team list failed sender=%s", sender)
        lines = "\n".join(f"• {_team_list_row_line(rd)}" for _rid, rd in rows)
        deps.send_to(sender, f"Your maintenance tickets:\n{lines}")


def _send_team_ticket_template(
    sender: str, deps: MaintenanceDeps, request_id: str, rd: dict
) -> bool:
    if not _is_maintenance_assignee(sender, rd, deps.same_whatsapp):
        deps.send_to(sender, _assignee_close_denied_message(rd, sender, deps))
        return True
    status = _request_status(rd)
    if status == "AWAITING_USER_CLOSE":
        employee = (rd.get("employee") or "").strip()
        if employee:
            _notify_supervisor_close_request(
                employee, rd, request_id, sender, deps
            )
            deps.send_to(
                sender,
                "Close request sent to the supervisor again. Thank you.",
            )
        else:
            deps.send_to(sender, "No supervisor contact on this request.")
        return True
    if status != "ASSIGNED":
        deps.send_to(sender, f"Request is already {status.lower()}.")
        return True
    _notify_assignee(deps, rd, request_id, sender)
    return True


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
        try_start_maintenance_list(sender, deps)
        return True

    request_id = _parse_maintenance_action(incoming, "MTEAM_")
    if request_id:
        loaded = _load_request(deps.db, request_id)
        if not loaded:
            deps.send_to(sender, "Maintenance request not found.")
            return True
        _ref, rd = loaded
        _send_team_ticket_template(sender, deps, request_id, rd)
        return True
    return False


def _send_manage_actions(
    sender: str, deps: MaintenanceDeps, request_id: str, rd: dict
) -> None:
    status = _request_status(rd)
    if status not in ("PENDING", "ASSIGNED"):
        deps.send_to(
            sender,
            f"{_manage_row_title(rd)}\n\nStatus: {status.lower()}.",
        )
        deps.clear_session(sender)
        return
    if not _send_manager_ticket_template_to(sender, rd, request_id, deps, context="list"):
        deps.send_to(
            sender,
            "Could not load ticket details. Please try Maintenance - List again.",
        )


def handle_manager_manage_gate(
    sender: str,
    incoming: str,
    deps: MaintenanceDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not is_maintenance_manager(sender, deps.same_whatsapp):
        return False

    if incoming.strip().upper() in (
        "MAINTENANCE_MANAGE",
        "MAINTENANCE_MANAGE_MENU",
        "MAINTENANCE_LIST",
        "MAINTENANCE_LIST_MENU",
    ):
        try_start_maintenance_list(sender, deps)
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
    deps.send_to(sender, "Please use Re Assign on the maintenance ticket template.")


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


def _is_maintenance_assignee(sender: str, rd: dict, same_whatsapp: Callable) -> bool:
    assignee_wa = (rd.get("assigned_to_wa") or "").strip()
    return bool(assignee_wa and same_whatsapp(sender, assignee_wa))


def _assignee_close_denied_message(rd: dict, sender: str, deps: MaintenanceDeps) -> str:
    current_name = (rd.get("assigned_to") or "another technician").strip().title()
    prev_wa = (rd.get("previous_assignee_wa") or "").strip()
    status = _request_status(rd)
    if prev_wa and deps.same_whatsapp(prev_wa, sender):
        if status == "COMPLETED":
            return (
                f"This request has already been re-assigned to {current_name} and is now closed. "
                "You cannot close this request."
            )
        if status == "AWAITING_USER_CLOSE":
            return (
                f"This request has already been re-assigned to {current_name} and is awaiting "
                "supervisor confirmation. You cannot close this request."
            )
        return (
            f"This request has already been re-assigned to {current_name}. "
            "You cannot close this request."
        )
    exists, ud = get_user_record(sender)
    if exists and is_maintenance_team_user(ud):
        return (
            f"This request is assigned to {current_name}. "
            "You are not authorized to close it."
        )
    return "You are not authorized to close this request."


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


def _find_supervisor_closable_request(
    db: object, supervisor_wa: str, same_whatsapp: Callable[[str, str], bool]
) -> str:
    """Most recent AWAITING_USER_CLOSE maintenance request for this supervisor."""
    best_id = ""
    best_ts = None
    try:
        snaps = db.collection("requests").where("type", "==", "MAINTENANCE").stream()
    except Exception:
        logger.exception("maintenance supervisor closable query failed")
        return ""
    for snap in snaps:
        rd = snap.to_dict() or {}
        if _request_status(rd) != "AWAITING_USER_CLOSE":
            continue
        wa = (rd.get("employee") or "").strip()
        if not wa or not same_whatsapp(supervisor_wa, wa):
            continue
        ts = rd.get("technician_closed_at") or rd.get("requested_datetime")
        if best_ts is None or (ts and ts > best_ts):
            best_ts = ts
            best_id = snap.id
    return best_id


def _user_close_template_name() -> str:
    return (
        os.getenv("MAINTENANCE_USER_CLOSE_TEMPLATE_NAME")
        or "maintenance_tkt_close_v2"
    ).strip()


def _user_close_template_language() -> str:
    return (
        os.getenv("MAINTENANCE_USER_CLOSE_TEMPLATE_LANGUAGE_CODE") or "en"
    ).strip()


def _user_close_template_body_fields() -> list[str]:
    raw = (
        os.getenv("MAINTENANCE_USER_CLOSE_TEMPLATE_BODY_FIELDS")
        or "machine,issue,addressed_by"
    ).strip()
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _user_close_template_body_values(
    rd: dict, technician_wa: str, deps: MaintenanceDeps
) -> list[str]:
    values = {
        "machine": (rd.get("machine_no_label") or "—").strip(),
        "issue": (rd.get("issue_category_label") or "—").strip(),
        "addressed_by": (rd.get("assigned_to") or "—").strip(),
        "machine_no": (rd.get("machine_no_label") or "—").strip(),
        "technician": (rd.get("assigned_to") or "—").strip(),
    }
    if technician_wa and values.get("addressed_by") in ("—", ""):
        exists, ud = get_user_record(technician_wa)
        if exists and ud:
            values["addressed_by"] = (ud.get("name") or "Technician").strip()
    fields = _user_close_template_body_fields()
    return [values.get(key, "—")[:1024] for key in fields]


def _notify_supervisor_close_request(
    supervisor_wa: str,
    rd: dict,
    request_id: str,
    technician_wa: str,
    deps: MaintenanceDeps,
) -> bool:
    template_name = _user_close_template_name()
    if not template_name:
        logger.error(
            "MAINTENANCE_USER_CLOSE_TEMPLATE_NAME not set request_id=%s",
            request_id,
        )
        return False
    phone = wa_id_to_phone(supervisor_wa)
    try:
        ensure_customer(phone, name=(rd.get("employee_name") or "Supervisor"))
        send_template(
            phone,
            template_name,
            language_code=_user_close_template_language(),
            body_values=_user_close_template_body_values(rd, technician_wa, deps),
            callback_data=request_id[:512],
            ensure_contact=False,
        )
        logger.info("maintenance user close template sent request_id=%s", request_id)
        return True
    except Exception:
        logger.exception(
            "maintenance user close template failed request_id=%s", request_id
        )
        return False


def _jmd_md_assign_template_name() -> str:
    return (
        os.getenv("MAINTENANCE_JMD_MD_ASSIGN_TEMPLATE_NAME")
        or os.getenv("MAINTENANCE_JMD_MD_CLOSE_TEMPLATE_NAME")
        or "maintenance_jmd_md_notification"
    ).strip()


def _jmd_md_assign_template_language() -> str:
    return (
        os.getenv("MAINTENANCE_JMD_MD_ASSIGN_TEMPLATE_LANGUAGE_CODE")
        or os.getenv("MAINTENANCE_JMD_MD_CLOSE_TEMPLATE_LANGUAGE_CODE")
        or "en"
    ).strip()


def _jmd_md_assign_template_body_fields() -> list[str]:
    raw = (
        os.getenv("MAINTENANCE_JMD_MD_ASSIGN_TEMPLATE_BODY_FIELDS")
        or "requester,unit,department,machine,issue,requested_at,assigned_to"
    ).strip()
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _jmd_md_assign_template_body_values(rd: dict) -> list[str]:
    values = {
        "requester": (rd.get("employee_name") or "—").strip(),
        "unit": _unit_label(rd.get("jmd_route") or ""),
        "department": (rd.get("department") or "—").strip(),
        "machine": (rd.get("machine_no_label") or "—").strip(),
        "machine_no": (rd.get("machine_no_label") or "—").strip(),
        "issue": (rd.get("issue_category_label") or "—").strip(),
        "assigned_to": (rd.get("assigned_to") or "—").strip(),
        "addressed_by": (rd.get("assigned_to") or "—").strip(),
        "requested_at": _format_ist(rd.get("requested_datetime")) or "—",
    }
    fields = _jmd_md_assign_template_body_fields()
    return [values.get(key, "—")[:1024] for key in fields]


def _notify_jmd_md_assignment(
    rd: dict,
    request_id: str,
    deps: MaintenanceDeps,
) -> None:
    """Notify unit JMD + MD when a maintenance request is assigned to a technician."""
    template_name = _jmd_md_assign_template_name()
    if not template_name:
        logger.error(
            "MAINTENANCE_JMD_MD_ASSIGN_TEMPLATE_NAME not set request_id=%s",
            request_id,
        )
        return
    route = rd.get("jmd_route") or ""
    recipients = _jmd_md_notify_recipients(route, deps.same_whatsapp)
    if not recipients:
        logger.warning(
            "maintenance JMD/MD assign notify skipped — no recipients "
            "request_id=%s route=%s",
            request_id,
            route,
        )
        return
    body_values = _jmd_md_assign_template_body_values(rd)
    for wa in recipients:
        phone = wa_id_to_phone(wa)
        try:
            ensure_customer(phone, name="Approver")
            send_template(
                phone,
                template_name,
                language_code=_jmd_md_assign_template_language(),
                body_values=body_values,
                callback_data=request_id[:512],
                ensure_contact=False,
            )
            logger.info(
                "maintenance JMD/MD assign template sent wa=%s request_id=%s",
                wa,
                request_id,
            )
        except Exception:
            logger.exception(
                "maintenance JMD/MD assign template failed wa=%s request_id=%s",
                wa,
                request_id,
            )


def handle_maintenance_assignee_gate(
    sender: str,
    incoming: str,
    deps: MaintenanceDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    request_id = _parse_maintenance_action(incoming, "MMAINT_CLOSED_")
    if not request_id:
        cb = _normalize_maint_callback_request_id(callback_request_id)
        if _is_closed_label(incoming) and cb:
            request_id = cb
    if not request_id and _is_closed_label(incoming):
        request_id = _find_assignee_closable_request(
            deps.db, sender, deps.same_whatsapp
        )
    if not request_id:
        if _is_closed_label(incoming):
            return False
        return False

    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Maintenance request not found.")
        return True
    ref, rd = loaded
    if not _is_maintenance_assignee(sender, rd, deps.same_whatsapp):
        if _is_closed_label(incoming) or request_id:
            deps.send_to(sender, _assignee_close_denied_message(rd, sender, deps))
            return True
        return False
    if _request_status(rd) != "ASSIGNED":
        deps.send_to(sender, f"Request is already {_request_status(rd).lower()}.")
        return True

    ref.update({
        "maintenance_status": "AWAITING_USER_CLOSE",
        "technician_closed_at": deps.utcnow(),
        "technician_closed_by": sender,
    })
    deps.clear_session(sender)
    employee = (rd.get("employee") or "").strip()
    if employee:
        if _notify_supervisor_close_request(
            employee, rd, request_id, sender, deps
        ):
            deps.send_to(
                sender, "Supervisor notified to confirm closure. Thank you."
            )
        else:
            deps.send_to(
                sender,
                "Could not send the close request to the supervisor. "
                "Please try again or contact admin.",
            )
    else:
        deps.send_to(sender, "No supervisor contact on this request.")
    return True


def handle_maintenance_user_close_gate(
    sender: str,
    incoming: str,
    deps: MaintenanceDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not _is_user_close_label(incoming) and not _parse_maintenance_action(
        incoming, "MMAINT_USER_CLOSE_"
    ):
        return False

    request_id = _parse_maintenance_action(incoming, "MMAINT_USER_CLOSE_")
    if not request_id:
        cb = _normalize_maint_callback_request_id(callback_request_id)
        if _is_user_close_label(incoming) and cb:
            request_id = cb
    if not request_id and _is_user_close_label(incoming):
        request_id = _find_supervisor_closable_request(
            deps.db, sender, deps.same_whatsapp
        )
    if not request_id:
        return False

    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Maintenance request not found.")
        return True
    ref, rd = loaded
    if not deps.same_whatsapp(rd.get("employee"), sender):
        deps.send_to(sender, "This maintenance request does not belong to you.")
        return True
    if _request_status(rd) != "AWAITING_USER_CLOSE":
        deps.send_to(sender, "This ticket is not awaiting your confirmation.")
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
    deps.clear_session(sender)
    tech_wa = (rd.get("assigned_to_wa") or "").strip()
    msg = "Your maintenance request has been closed. Thank you."
    if time_taken:
        msg = (
            f"Your maintenance request has been closed.\n"
            f"Time taken: {time_taken}. Thank you."
        )
    deps.send_to(sender, msg)
    if tech_wa:
        tech_msg = (
            f"Maintenance ticket from {rd.get('employee_name') or 'supervisor'} "
            "has been closed."
        )
        if time_taken:
            tech_msg += f"\nTime taken: {time_taken}."
        deps.send_to(tech_wa, tech_msg)
    return True


def _is_maintenance_close_incoming(incoming: str) -> bool:
    return (
        _is_closed_label(incoming)
        or _is_user_close_label(incoming)
        or bool(_parse_maintenance_action(incoming, "MMAINT_CLOSED_"))
        or bool(_parse_maintenance_action(incoming, "MMAINT_USER_CLOSE_"))
    )


def handle_maintenance_close_gates(
    sender: str,
    incoming: str,
    deps: MaintenanceDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    """Maintenance-only close actions (technician Closed, supervisor Close Ticket)."""
    if not _is_maintenance_close_incoming(incoming):
        return False
    cb_rid = _normalize_maint_callback_request_id(callback_request_id)
    if cb_rid:
        rtype = request_type_for_id(deps.db, cb_rid)
        if rtype and rtype != "MAINTENANCE":
            return False
    if handle_maintenance_assignee_gate(
        sender, incoming, deps, callback_request_id=callback_request_id
    ):
        return True
    if handle_maintenance_user_close_gate(
        sender, incoming, deps, callback_request_id=callback_request_id
    ):
        return True
    return False


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
        try_start_maintenance_list(sender, deps)
        return

    exists, ud = get_user_record(sender)
    if exists and is_maintenance_team_user(ud):
        deps.send_to(
            sender,
            "Maintenance team cannot raise new requests.\n"
            "Use Maintenance - List to view and close assigned jobs.",
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
