"""Tool Breakdown request flow — WhatsApp Form for shop-floor supervisors."""

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
    wa_from_10,
)
from interakt_api import (
    ensure_customer,
    send_list_menu_paged,
    send_template_with_image_header,
    send_tool_breakdown_flow_form,
    wa_id_to_phone,
)
from maintenance_data import MACHINE_LABELS, machine_no_options
from tool_breakdown_data import (
    ISSUE_LABELS,
    issue_options,
    is_supported_department,
    normalize_tool_name,
)

logger = logging.getLogger(__name__)

SESSION_WAITING_TOOL_BREAKDOWN_MANAGER_PRIORITY = (
    "WAITING_TOOL_BREAKDOWN_MANAGER_PRIORITY"
)
SESSION_WAITING_TOOL_BREAKDOWN_TOOL_ROOM_CLOSE = (
    "WAITING_TOOL_BREAKDOWN_TOOL_ROOM_CLOSE"
)

UNSUPPORTED_DEPT_MSG = (
    "Tool Breakdown form is available for PDC, Secondary, and Fettling departments only."
)
SUPERVISOR_ONLY_MSG = "Tool Breakdown form is available for supervisors only."

_PRIORITY_LABELS = {
    "TB_PRIO_HIGH": "High",
    "TB_PRIO_MED": "Medium",
    "TB_PRIO_LOW": "Low",
    "HIGH": "High",
    "MEDIUM": "Medium",
    "LOW": "Low",
}


def _format_repeated_display(count: object) -> str:
    """Template display: '0 Times', '2 Times', etc."""
    try:
        n = int(count)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        text = str(count or "0").strip()
        if text.lower().endswith("time"):
            return text
        return f"{text} Times"
    return f"{n} Times"


def _is_supervisor(ud: dict | None) -> bool:
    return bool(ud and ud.get("is_supervisor"))


def show_tool_breakdown_menu_for_user(user_data: dict | None) -> bool:
    if not tool_breakdown_flow_enabled():
        return False
    if not _is_supervisor(user_data):
        return False
    dept = _normalize_dept((user_data or {}).get("department") or "")
    return is_supported_department(dept)


def show_tool_breakdown_list_menu(
    wa_id: str, same_whatsapp: Callable[[str, str], bool]
) -> bool:
    if not tool_breakdown_flow_enabled():
        return False
    return is_tool_breakdown_manager(wa_id, same_whatsapp) or is_tool_room_user(
        wa_id, same_whatsapp
    )


@dataclass
class ToolBreakdownDeps:
    db: object
    send_to: Callable[[str, str], None]
    session_merge: Callable[..., None]
    session_ref: Callable[[str], object]
    utcnow: Callable
    clear_session: Callable[[str], None]
    go_main_menu: Callable[[str], None]
    same_whatsapp: Callable[[str, str], bool]
    has_active_whatsapp_session: Callable[[str], bool]


def tool_breakdown_flow_template_name() -> str:
    return (os.getenv("TOOL_BREAKDOWN_FLOW_TEMPLATE_NAME") or "").strip()


def tool_breakdown_flow_enabled() -> bool:
    return bool(tool_breakdown_flow_template_name())


def _normalize_dept(dept: str) -> str:
    d = (dept or "").strip().upper()
    if d == "FET":
        return "FETTLING"
    return d


def _normalize_id(raw: str) -> str:
    return (raw or "").strip().lower().replace(" ", "_").replace("-", "_")


def _flow_pick(data: dict, *keys: str) -> str:
    for key in keys:
        val = data.get(key)
        if isinstance(val, dict):
            val = val.get("id") or val.get("title")
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


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


def _issue_photo_from_flow_data(data: dict) -> object | None:
    from it_flow_media import deep_find_issue_photo

    return deep_find_issue_photo(data)


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
    if not dept or not is_supported_department(dept):
        return None

    machine_no = _normalize_id(_flow_pick(data, "machine_no"))
    issue = _normalize_id(_flow_pick(data, "issue_category"))
    tool_name = (_flow_pick(data, "tool_name") or "").strip()
    line_stop = _normalize_id(_flow_pick(data, "line_stop_threat"))

    if not machine_no or not issue or not tool_name:
        return None
    if line_stop not in ("yes", "no"):
        return None

    allowed_machines = {m["id"] for m in machine_no_options(dept, route)}
    if machine_no not in allowed_machines:
        return None

    allowed_issues = {m["id"] for m in issue_options(dept)}
    if issue not in allowed_issues:
        return None

    return {
        "machine_no": machine_no,
        "machine_no_label": MACHINE_LABELS.get(machine_no, machine_no),
        "tool_name": tool_name,
        "tool_name_normalized": normalize_tool_name(tool_name),
        "issue_category": issue,
        "issue_category_label": ISSUE_LABELS.get(issue, issue),
        "line_stop_threat": line_stop,
        "line_stop_threat_label": "Yes" if line_stop == "yes" else "No",
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
            os.getenv("TOOL_BREAKDOWN_MANAGER_UNIT_II_WHATSAPP_NUMBER")
            or os.getenv("TOOL_BREAKDOWN_MANAGER_2_WHATSAPP_NUMBER")
            or ""
        ).strip()
    else:
        raw = (
            os.getenv("TOOL_BREAKDOWN_MANAGER_UNIT_I_WHATSAPP_NUMBER")
            or os.getenv("TOOL_BREAKDOWN_MANAGER_1_WHATSAPP_NUMBER")
            or ""
        ).strip()
    if not raw:
        return ""
    return wa_from_10(wa_id_to_phone(raw)[-10:])


def _tool_room_wa(route: str) -> str:
    key = _normalize_route(route)
    if key == "JMD2":
        raw = (
            os.getenv("TOOL_ROOM_UNIT_II_WHATSAPP_NUMBER")
            or os.getenv("TOOL_ROOM_2_WHATSAPP_NUMBER")
            or ""
        ).strip()
    else:
        raw = (
            os.getenv("TOOL_ROOM_UNIT_I_WHATSAPP_NUMBER")
            or os.getenv("TOOL_ROOM_1_WHATSAPP_NUMBER")
            or ""
        ).strip()
    if not raw:
        return ""
    return wa_from_10(wa_id_to_phone(raw)[-10:])


def _manager_template_name(route: str = "") -> str:
    del route
    return (
        os.getenv("TOOL_BREAKDOWN_MANAGER_NOTIFICATION_TEMPLATE_NAME")
        or os.getenv("TOOL_BREAKDOWN_MANAGER_TEMPLATE_NAME")
        or "tool_breakdown_manager_notification_v01"
    ).strip()


def _manager_template_language() -> str:
    return (
        os.getenv("TOOL_BREAKDOWN_MANAGER_TEMPLATE_LANGUAGE_CODE") or "en"
    ).strip()


def _manager_template_body_fields() -> list[str]:
    raw = (
        os.getenv("TOOL_BREAKDOWN_MANAGER_TEMPLATE_BODY_FIELDS")
        or "employee,unit,department,machine,tool,issue,repeated,line_stop,requested_at"
    ).strip()
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _manager_template_body_values(rd: dict) -> list[str]:
    values = {
        "employee": (rd.get("employee_name") or "Employee").strip(),
        "unit": _unit_label(rd.get("jmd_route") or ""),
        "department": (rd.get("department") or "—").strip(),
        "machine": (rd.get("machine_no_label") or "—").strip(),
        "tool": (rd.get("tool_name") or "—").strip(),
        "issue": (rd.get("issue_category_label") or "—").strip(),
        "repeated": _format_repeated_display(rd.get("repeated_count", 0)),
        "line_stop": (rd.get("line_stop_threat_label") or "—").strip(),
        "requested_at": _format_ist(rd.get("requested_datetime")) or "—",
    }
    fields = _manager_template_body_fields()
    return [values.get(key, "—")[:1024] for key in fields]


def _tool_room_template_name() -> str:
    return (
        os.getenv("TOOL_BREAKDOWN_TOOL_ROOM_TEMPLATE_NAME")
        or os.getenv("TOOL_ROOM_NOTIFY_TEMPLATE_NAME")
        or "tool_breakdown_tool_room_notification_v01"
    ).strip()


def _tool_room_template_language() -> str:
    return (
        os.getenv("TOOL_BREAKDOWN_TOOL_ROOM_TEMPLATE_LANGUAGE_CODE")
        or os.getenv("TOOL_ROOM_NOTIFY_TEMPLATE_LANGUAGE_CODE")
        or "en"
    ).strip()


def _tool_room_template_body_fields() -> list[str]:
    raw = (
        os.getenv("TOOL_BREAKDOWN_TOOL_ROOM_TEMPLATE_BODY_FIELDS")
        or os.getenv("TOOL_ROOM_NOTIFY_TEMPLATE_BODY_FIELDS")
        or "priority,employee,unit,department,machine,tool,issue,repeated,line_stop,requested_at"
    ).strip()
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _tool_room_template_body_values(rd: dict) -> list[str]:
    route = _normalize_route(rd.get("jmd_route") or "")
    priority = (rd.get("priority_label") or rd.get("priority") or "—").strip()
    values = {
        "priority": priority,
        "employee": (rd.get("employee_name") or "—").strip(),
        "unit": _unit_label(route),
        "department": (rd.get("department") or "—").strip(),
        "machine": (rd.get("machine_no_label") or "—").strip(),
        "tool": (rd.get("tool_name") or "—").strip(),
        "issue": (rd.get("issue_category_label") or "—").strip(),
        "repeated": _format_repeated_display(rd.get("repeated_count", 0)),
        "line_stop": (rd.get("line_stop_threat_label") or "—").strip(),
        "requested_at": _format_ist(rd.get("requested_datetime")) or "—",
    }
    fields = _tool_room_template_body_fields()
    return [values.get(key, "—")[:1024] for key in fields]


def _manager_route_for_sender(
    sender: str, same_whatsapp: Callable[[str, str], bool]
) -> str:
    if same_whatsapp(sender, _manager_wa("JMD1")):
        return "JMD1"
    if same_whatsapp(sender, _manager_wa("JMD2")):
        return "JMD2"
    return ""


def is_tool_breakdown_manager(
    sender: str, same_whatsapp: Callable[[str, str], bool]
) -> bool:
    return bool(_manager_route_for_sender(sender, same_whatsapp))


def is_tool_room_user(
    sender: str, same_whatsapp: Callable[[str, str], bool]
) -> bool:
    return bool(_tool_room_route_for_sender(sender, same_whatsapp))


def _tool_room_route_for_sender(
    sender: str, same_whatsapp: Callable[[str, str], bool]
) -> str:
    if same_whatsapp(sender, _tool_room_wa("JMD1")):
        return "JMD1"
    if same_whatsapp(sender, _tool_room_wa("JMD2")):
        return "JMD2"
    return ""


def _request_status(rd: dict) -> str:
    return (rd.get("tool_breakdown_status") or "PENDING").strip().upper()


def is_tool_breakdown_manager_priority_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_TOOL_BREAKDOWN_MANAGER_PRIORITY


def is_tool_breakdown_tool_room_close_state(state: str | None) -> bool:
    return (state or "").strip() == SESSION_WAITING_TOOL_BREAKDOWN_TOOL_ROOM_CLOSE


def _load_request(db: object, request_id: str) -> tuple[object, dict] | None:
    rid = (request_id or "").strip()
    if not rid:
        return None
    ref = db.collection("requests").document(rid)
    snap = ref.get()
    if not snap.exists:
        return None
    rd = snap.to_dict() or {}
    if (rd.get("type") or "").strip().upper() != "TOOL_BREAKDOWN":
        return None
    return ref, rd


def count_repeated_breakdowns(
    db: object,
    *,
    tool_name_normalized: str,
    issue_category: str,
    department: str,
) -> int:
    dept = _normalize_dept(department)
    tool_key = (tool_name_normalized or "").strip()
    issue_key = (issue_category or "").strip()
    if not tool_key or not issue_key or not dept:
        return 0
    count = 0
    try:
        snaps = db.collection("requests").where("type", "==", "TOOL_BREAKDOWN").stream()
    except Exception:
        logger.exception("tool breakdown repeated-count query failed")
        return 0
    for snap in snaps:
        rd = snap.to_dict() or {}
        if _normalize_dept(rd.get("department") or "") != dept:
            continue
        if (rd.get("issue_category") or "") != issue_key:
            continue
        if (rd.get("tool_name_normalized") or "") != tool_key:
            continue
        count += 1
    return count


def _issue_photo_url(rd: dict) -> str:
    photo = (rd.get("issue_photo_url") or "").strip()
    if photo.lower().startswith("https://"):
        return photo
    return ""


def _set_pending_manager_priority(
    deps: ToolBreakdownDeps, manager_wa: str, request_id: str
) -> None:
    deps.session_merge(
        manager_wa,
        state=SESSION_WAITING_TOOL_BREAKDOWN_MANAGER_PRIORITY,
        tool_breakdown_manager_request_id=(request_id or "").strip(),
    )


def _pending_manager_request_id(sender: str, deps: ToolBreakdownDeps) -> str:
    snap = deps.session_ref(sender).get()
    if not snap.exists:
        return ""
    data = snap.to_dict() or {}
    if data.get("state") != SESSION_WAITING_TOOL_BREAKDOWN_MANAGER_PRIORITY:
        return ""
    return (data.get("tool_breakdown_manager_request_id") or "").strip()


def _normalize_tb_callback_request_id(callback_request_id: str) -> str:
    raw = normalize_callback_request_id(callback_request_id)
    if raw in (
        "tool-breakdown-flow",
        "tool_breakdown_form",
        "tool_breakdown",
        "tool-breakdown-list",
        "tool-breakdown-manager-list",
        "tool-breakdown-tool-room-list",
    ):
        return ""
    return raw


def _parse_tb_action(incoming: str, prefix: str) -> str | None:
    raw = (incoming or "").strip()
    upper = raw.upper()
    p = prefix.upper()
    if not upper.startswith(p):
        return None
    rid = raw[len(prefix) :].strip()
    return rid or None


def _is_closed_label(raw: str) -> bool:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key in ("closed", "done", "close")


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
    remainder = parts[len(title_parts) :]
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
    tool = (rd.get("tool_name") or "—").strip()
    if len(tool) > 14:
        tool = tool[:14] + "…"
    issue = (rd.get("issue_category_label") or "—").strip()
    priority = (rd.get("priority_label") or "—").strip()
    return f"{tool} - {issue} - {priority}"


def _manager_list_row_extra(rd: dict) -> str:
    bits: list[str] = []
    machine = (rd.get("machine_no_label") or "").strip()
    if machine:
        bits.append(machine)
    dept = (rd.get("department") or "").strip()
    if dept:
        bits.append(dept)
    requested = _format_ist(rd.get("requested_datetime"))
    if requested:
        bits.append(requested)
    return " | ".join(bits)[:72]


def _manager_list_row_fields(request_id: str, rd: dict) -> dict[str, str]:
    row = _split_list_row(f"TB_MGR_{request_id}"[:256], _manager_list_row_line(rd))
    return _merge_list_row_description(row, _manager_list_row_extra(rd))


def _tool_room_list_row_line(rd: dict) -> str:
    machine = (rd.get("machine_no_label") or "—").strip()
    tool = (rd.get("tool_name") or "—").strip()
    if len(tool) > 10:
        tool = tool[:10] + "…"
    priority = (rd.get("priority_label") or "—").strip()
    return f"{machine} - {tool} - {priority}"


def _tool_room_list_row_extra(rd: dict) -> str:
    bits: list[str] = []
    issue = (rd.get("issue_category_label") or "").strip()
    if issue:
        bits.append(issue)
    requester = (rd.get("employee_name") or "").strip()
    if requester:
        bits.append(requester)
    requested = _format_ist(rd.get("requested_datetime"))
    if requested:
        bits.append(requested)
    return " | ".join(bits)[:72]


def _tool_room_list_row_fields(request_id: str, rd: dict) -> dict[str, str]:
    row = _split_list_row(f"TB_ROOM_{request_id}"[:256], _tool_room_list_row_line(rd))
    return _merge_list_row_description(row, _tool_room_list_row_extra(rd))


def _fetch_manager_priority_list(
    db: object, route: str
) -> list[tuple[str, dict]]:
    norm_route = _normalize_route(route)
    rows: list[tuple[str, dict]] = []
    try:
        snaps = db.collection("requests").where("type", "==", "TOOL_BREAKDOWN").stream()
    except Exception:
        logger.exception("tool breakdown manager list query failed")
        return rows
    for snap in snaps:
        rd = snap.to_dict() or {}
        if _request_status(rd) != "PRIORITY_SET":
            continue
        if _normalize_route(rd.get("jmd_route") or "") != norm_route:
            continue
        rows.append((snap.id, rd))
    rows.sort(
        key=lambda item: item[1].get("priority_set_at")
        or item[1].get("requested_datetime")
        or "",
        reverse=True,
    )
    return rows[:15]


def _fetch_tool_room_assigned_list(
    db: object, route: str
) -> list[tuple[str, dict]]:
    norm_route = _normalize_route(route)
    rows: list[tuple[str, dict]] = []
    try:
        snaps = db.collection("requests").where("type", "==", "TOOL_BREAKDOWN").stream()
    except Exception:
        logger.exception("tool breakdown tool room list query failed")
        return rows
    for snap in snaps:
        rd = snap.to_dict() or {}
        if _request_status(rd) != "PRIORITY_SET":
            continue
        if _normalize_route(rd.get("jmd_route") or "") != norm_route:
            continue
        rows.append((snap.id, rd))
    rows.sort(
        key=lambda item: item[1].get("priority_set_at")
        or item[1].get("requested_datetime")
        or "",
        reverse=True,
    )
    return rows[:15]


def _set_pending_tool_room_close(
    deps: ToolBreakdownDeps, room_wa: str, request_id: str
) -> None:
    deps.session_merge(
        room_wa,
        state=SESSION_WAITING_TOOL_BREAKDOWN_TOOL_ROOM_CLOSE,
        tool_breakdown_tool_room_request_id=(request_id or "").strip(),
    )


def _pending_tool_room_request_id(sender: str, deps: ToolBreakdownDeps) -> str:
    snap = deps.session_ref(sender).get()
    if not snap.exists:
        return ""
    data = snap.to_dict() or {}
    if data.get("state") != SESSION_WAITING_TOOL_BREAKDOWN_TOOL_ROOM_CLOSE:
        return ""
    return (data.get("tool_breakdown_tool_room_request_id") or "").strip()


def _parse_priority_action(incoming: str) -> str | None:
    raw = (incoming or "").strip().upper().replace(" ", "_")
    if raw in ("TB_PRIO_HIGH", "HIGH", "PRIORITY_HIGH"):
        return "HIGH"
    if raw in ("TB_PRIO_MED", "MEDIUM", "PRIORITY_MEDIUM", "MED"):
        return "MEDIUM"
    if raw in ("TB_PRIO_LOW", "LOW", "PRIORITY_LOW"):
        return "LOW"
    return None


def _is_priority_label(incoming: str) -> bool:
    return _parse_priority_action(incoming) is not None


def _send_manager_ticket_template_to(
    recipient_wa: str,
    rd: dict,
    request_id: str,
    deps: ToolBreakdownDeps,
    *,
    context: str,
) -> bool:
    photo_url = _issue_photo_url(rd)
    if not photo_url:
        logger.error(
            "tool breakdown manager notify skipped — no photo request_id=%s",
            request_id,
        )
        return False

    template_name = _manager_template_name(rd.get("jmd_route") or "")
    if not template_name:
        logger.warning(
            "tool breakdown manager template not configured request_id=%s", request_id
        )
        return False

    body_values = _manager_template_body_values(rd)
    phone = wa_id_to_phone(recipient_wa)
    try:
        ensure_customer(phone, name="Tool Maintenance Manager")
        send_template_with_image_header(
            phone,
            template_name,
            photo_url,
            language_code=_manager_template_language(),
            body_values=body_values,
            callback_data=request_id[:512],
            ensure_contact=False,
        )
        if is_tool_breakdown_manager(recipient_wa, deps.same_whatsapp):
            _set_pending_manager_priority(deps, recipient_wa, request_id)
        logger.info(
            "tool breakdown manager template sent context=%s request_id=%s",
            context,
            request_id,
        )
        return True
    except Exception:
        logger.exception(
            "tool breakdown manager template failed context=%s request_id=%s",
            context,
            request_id,
        )
        return False


def _notify_tool_breakdown_manager(
    deps: ToolBreakdownDeps, rd: dict, request_id: str
) -> None:
    route = _normalize_route(rd.get("jmd_route") or "")
    mgr = _manager_wa(route)
    if not mgr:
        logger.warning(
            "tool breakdown manager WA not set route=%s request_id=%s",
            route,
            request_id,
        )
        return
    _send_manager_ticket_template_to(mgr, rd, request_id, deps, context="new_ticket")


def _send_tool_room_ticket_template_to(
    recipient_wa: str,
    rd: dict,
    request_id: str,
    deps: ToolBreakdownDeps,
    *,
    context: str,
) -> bool:
    photo_url = _issue_photo_url(rd)
    if not photo_url:
        logger.error(
            "tool room notify skipped — no photo request_id=%s", request_id
        )
        return False

    template_name = _tool_room_template_name()
    if not template_name:
        logger.warning(
            "tool room template not configured request_id=%s", request_id
        )
        return False

    body_values = _tool_room_template_body_values(rd)
    try:
        ensure_customer(wa_id_to_phone(recipient_wa), name="Tool Room")
        send_template_with_image_header(
            wa_id_to_phone(recipient_wa),
            template_name,
            photo_url,
            language_code=_tool_room_template_language(),
            body_values=body_values,
            callback_data=request_id[:512],
            ensure_contact=False,
        )
        if is_tool_room_user(recipient_wa, deps.same_whatsapp):
            _set_pending_tool_room_close(deps, recipient_wa, request_id)
        logger.info(
            "tool room template sent context=%s request_id=%s template=%s",
            context,
            request_id,
            template_name,
        )
        return True
    except Exception:
        logger.exception("tool room template failed request_id=%s", request_id)
        return False


def _notify_tool_room(deps: ToolBreakdownDeps, rd: dict, request_id: str) -> None:
    route = _normalize_route(rd.get("jmd_route") or "")
    room_wa = _tool_room_wa(route)
    if not room_wa:
        logger.warning(
            "tool room WA not set route=%s request_id=%s", route, request_id
        )
        return
    _send_tool_room_ticket_template_to(
        room_wa, rd, request_id, deps, context="priority_set"
    )


def _complete_priority_assignment(
    sender: str,
    request_id: str,
    priority: str,
    deps: ToolBreakdownDeps,
) -> None:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Request not found.")
        return
    ref, rd = loaded

    mgr_route = _manager_route_for_sender(sender, deps.same_whatsapp)
    req_route = _normalize_route(rd.get("jmd_route") or "")
    if mgr_route and mgr_route != req_route:
        deps.send_to(sender, "This request belongs to the other unit.")
        return

    status = _request_status(rd)
    if status not in ("PENDING", "PRIORITY_SET", ""):
        deps.send_to(
            sender,
            f"This ticket is already {status.replace('_', ' ').title()}.",
        )
        return

    is_reprioritize = status == "PRIORITY_SET"
    label = _PRIORITY_LABELS.get(priority, priority.title())
    now = deps.utcnow()
    update = {
        "tool_breakdown_status": "PRIORITY_SET",
        "priority": priority,
        "priority_label": label,
        "priority_set_by": sender,
        "priority_set_at": now,
    }
    if is_reprioritize:
        update["priority_updated_at"] = now
        update["priority_updated_by"] = sender
    ref.update(update)
    rd.update(update)

    _notify_tool_room(deps, rd, request_id)
    if is_reprioritize:
        deps.send_to(
            sender,
            f"Priority updated to {label}.\nTool room has been notified.",
        )
        employee_msg = (
            "Your Tool Breakdown request priority was updated.\n\n"
            f"Tool: {rd.get('tool_name') or '—'}\n"
            f"Issue: {rd.get('issue_category_label') or '—'}\n"
            f"Priority: {label}"
        )
    else:
        deps.send_to(
            sender,
            f"Priority set to {label}.\nTool room has been notified.",
        )
        employee_msg = (
            "Your Tool Breakdown request has been prioritised.\n\n"
            f"Tool: {rd.get('tool_name') or '—'}\n"
            f"Issue: {rd.get('issue_category_label') or '—'}\n"
            f"Priority: {label}"
        )
    deps.send_to(rd.get("employee") or "", employee_msg)
    logger.info(
        "tool breakdown priority %s request_id=%s priority=%s by=%s",
        "updated" if is_reprioritize else "set",
        request_id,
        priority,
        sender,
    )


def handle_tool_breakdown_manager_gate(
    sender: str,
    incoming: str,
    deps: ToolBreakdownDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not is_tool_breakdown_manager(sender, deps.same_whatsapp):
        return False

    priority = _parse_priority_action(incoming)
    if not priority:
        if _is_priority_label(incoming):
            deps.send_to(
                sender,
                "Could not read priority. Tap High, Medium, or Low on the latest ticket.",
            )
            return True
        return False

    request_id = _normalize_tb_callback_request_id(callback_request_id)
    if not request_id:
        request_id = _pending_manager_request_id(sender, deps)
    if not request_id:
        deps.send_to(
            sender,
            "Could not identify which ticket to prioritise.\n"
            "Open the latest Tool Breakdown notification and tap a priority.",
        )
        return True

    _complete_priority_assignment(sender, request_id, priority, deps)
    return True


def handle_tool_breakdown_manager_priority_input(
    sender: str,
    incoming: str,
    session: dict,
    deps: ToolBreakdownDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not is_tool_breakdown_manager(sender, deps.same_whatsapp):
        return False
    if not is_tool_breakdown_manager_priority_state((session or {}).get("state")):
        return False

    priority = _parse_priority_action(incoming)
    if not priority:
        return False

    request_id = _normalize_tb_callback_request_id(callback_request_id)
    if not request_id:
        request_id = (session or {}).get("tool_breakdown_manager_request_id") or ""
    if not request_id:
        deps.send_to(
            sender,
            "Could not identify which ticket to prioritise.\n"
            "Open the latest Tool Breakdown notification and tap a priority.",
        )
        return True

    _complete_priority_assignment(sender, request_id.strip(), priority, deps)
    return True


def _complete_tool_room_close(
    sender: str, request_id: str, deps: ToolBreakdownDeps
) -> None:
    loaded = _load_request(deps.db, request_id)
    if not loaded:
        deps.send_to(sender, "Request not found.")
        return
    ref, rd = loaded

    room_route = _tool_room_route_for_sender(sender, deps.same_whatsapp)
    req_route = _normalize_route(rd.get("jmd_route") or "")
    if room_route and room_route != req_route:
        deps.send_to(sender, "This request belongs to the other unit.")
        return

    status = _request_status(rd)
    if status != "PRIORITY_SET":
        deps.send_to(
            sender,
            f"Request is already {status.replace('_', ' ').title()}.",
        )
        return

    now = deps.utcnow()
    ref.update(
        {
            "tool_breakdown_status": "CLOSED",
            "closed_by": sender,
            "closed_at": now,
        }
    )
    deps.clear_session(sender)
    deps.send_to(sender, "Tool Breakdown ticket closed. Thank you.")
    employee = (rd.get("employee") or "").strip()
    if employee:
        deps.send_to(
            employee,
            (
                "Your Tool Breakdown request has been closed.\n\n"
                f"Tool: {rd.get('tool_name') or '—'}\n"
                f"Issue: {rd.get('issue_category_label') or '—'}\n"
                f"Machine: {rd.get('machine_no_label') or '—'}"
            ),
        )
    logger.info("tool breakdown closed request_id=%s by=%s", request_id, sender)


def try_start_manager_list(sender: str, deps: ToolBreakdownDeps) -> None:
    route = _manager_route_for_sender(sender, deps.same_whatsapp)
    if not route:
        deps.send_to(sender, "Not authorized.")
        return
    rows = _fetch_manager_priority_list(deps.db, route)
    if not rows:
        deps.send_to(
            sender,
            "No priority-assigned Tool Breakdown requests for your unit.",
        )
        return
    list_rows = [_manager_list_row_fields(rid, rd) for rid, rd in rows]
    try:
        send_list_menu_paged(
            wa_id_to_phone(sender),
            "Tool Breakdown — priority assigned (tap to change priority):",
            list_rows,
            button_label="Open",
            section_title="Tickets",
            callback_data="tool-breakdown-manager-list",
        )
    except Exception:
        logger.exception("tool breakdown manager list failed sender=%s", sender)
        lines = "\n".join(f"• {_manager_list_row_line(rd)}" for _rid, rd in rows)
        deps.send_to(sender, f"Tool Breakdown tickets:\n{lines}")


def try_start_tool_room_list(sender: str, deps: ToolBreakdownDeps) -> None:
    route = _tool_room_route_for_sender(sender, deps.same_whatsapp)
    if not route:
        deps.send_to(sender, "Not authorized.")
        return
    rows = _fetch_tool_room_assigned_list(deps.db, route)
    if not rows:
        deps.send_to(
            sender,
            "No assigned Tool Breakdown requests for your unit.",
        )
        return
    list_rows = [_tool_room_list_row_fields(rid, rd) for rid, rd in rows]
    try:
        send_list_menu_paged(
            wa_id_to_phone(sender),
            "Tool Breakdown — assigned (tap to open and close):",
            list_rows,
            button_label="Open",
            section_title="Tickets",
            callback_data="tool-breakdown-tool-room-list",
        )
    except Exception:
        logger.exception("tool breakdown tool room list failed sender=%s", sender)
        lines = "\n".join(f"• {_tool_room_list_row_line(rd)}" for _rid, rd in rows)
        deps.send_to(sender, f"Assigned Tool Breakdown tickets:\n{lines}")


def try_start_tool_breakdown_list(sender: str, deps: ToolBreakdownDeps) -> None:
    if is_tool_breakdown_manager(sender, deps.same_whatsapp):
        try_start_manager_list(sender, deps)
        return
    if is_tool_room_user(sender, deps.same_whatsapp):
        try_start_tool_room_list(sender, deps)
        return
    deps.send_to(sender, "Not authorized.")


def handle_tool_breakdown_list_gate(
    sender: str,
    incoming: str,
    deps: ToolBreakdownDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    key = (incoming or "").strip().upper()
    if key in ("TOOL_BREAKDOWN_LIST", "TOOL_BREAKDOWN_LIST_MENU"):
        try_start_tool_breakdown_list(sender, deps)
        return True

    if is_tool_breakdown_manager(sender, deps.same_whatsapp):
        request_id = _parse_tb_action(incoming, "TB_MGR_")
        if request_id:
            loaded = _load_request(deps.db, request_id)
            if not loaded:
                deps.send_to(sender, "Tool Breakdown request not found.")
                return True
            _ref, rd = loaded
            mgr_route = _manager_route_for_sender(sender, deps.same_whatsapp)
            if _normalize_route(rd.get("jmd_route") or "") != mgr_route:
                deps.send_to(sender, "This request belongs to the other unit.")
                return True
            if _request_status(rd) != "PRIORITY_SET":
                deps.send_to(
                    sender,
                    f"Ticket status: {_request_status(rd).replace('_', ' ').title()}.",
                )
                return True
            if not _send_manager_ticket_template_to(
                sender, rd, request_id, deps, context="list_reprioritize"
            ):
                deps.send_to(
                    sender,
                    "Could not load ticket. Please try Tool - Breakdown List again.",
                )
            return True

    if is_tool_room_user(sender, deps.same_whatsapp):
        request_id = _parse_tb_action(incoming, "TB_ROOM_")
        if request_id:
            loaded = _load_request(deps.db, request_id)
            if not loaded:
                deps.send_to(sender, "Tool Breakdown request not found.")
                return True
            _ref, rd = loaded
            room_route = _tool_room_route_for_sender(sender, deps.same_whatsapp)
            if _normalize_route(rd.get("jmd_route") or "") != room_route:
                deps.send_to(sender, "This request belongs to the other unit.")
                return True
            if _request_status(rd) != "PRIORITY_SET":
                deps.send_to(
                    sender,
                    f"Ticket status: {_request_status(rd).replace('_', ' ').title()}.",
                )
                return True
            if not _send_tool_room_ticket_template_to(
                sender, rd, request_id, deps, context="list_open"
            ):
                deps.send_to(
                    sender,
                    "Could not load ticket. Please try Tool - Breakdown List again.",
                )
            return True

    return False


def handle_tool_breakdown_tool_room_close_gate(
    sender: str,
    incoming: str,
    deps: ToolBreakdownDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not is_tool_room_user(sender, deps.same_whatsapp):
        return False
    if not _is_closed_label(incoming):
        return False

    request_id = _parse_tb_action(incoming, "TB_CLOSED_")
    if not request_id:
        request_id = _normalize_tb_callback_request_id(callback_request_id)
    if not request_id:
        request_id = _pending_tool_room_request_id(sender, deps)
    if not request_id:
        deps.send_to(
            sender,
            "Could not identify which ticket to close.\n"
            "Use Tool - Breakdown List and open the ticket again.",
        )
        return True

    _complete_tool_room_close(sender, request_id.strip(), deps)
    return True


def handle_tool_breakdown_tool_room_close_input(
    sender: str,
    incoming: str,
    session: dict,
    deps: ToolBreakdownDeps,
    *,
    callback_request_id: str = "",
) -> bool:
    if not is_tool_room_user(sender, deps.same_whatsapp):
        return False
    if not is_tool_breakdown_tool_room_close_state((session or {}).get("state")):
        return False
    if not _is_closed_label(incoming):
        return False

    request_id = _normalize_tb_callback_request_id(callback_request_id)
    if not request_id:
        request_id = (session or {}).get("tool_breakdown_tool_room_request_id") or ""
    if not request_id:
        deps.send_to(
            sender,
            "Could not identify which ticket to close.\n"
            "Use Tool - Breakdown List and open the ticket again.",
        )
        return True

    _complete_tool_room_close(sender, request_id.strip(), deps)
    return True


def _employee_confirmation(rd: dict) -> str:
    return (
        "Your Tool Breakdown request has been submitted.\n\n"
        f"Machine: {rd.get('machine_no_label') or '—'}\n"
        f"Tool: {rd.get('tool_name') or '—'}\n"
        f"Issue: {rd.get('issue_category_label') or '—'}\n"
        f"Repeated (prior): {_format_repeated_display(rd.get('repeated_count', 0))}\n"
        f"Line stop threat: {rd.get('line_stop_threat_label') or '—'}"
    )


def try_start_form(sender: str, deps: ToolBreakdownDeps) -> None:
    if not tool_breakdown_flow_enabled():
        deps.send_to(
            sender,
            "Tool Breakdown form is not configured yet.\nPlease contact admin.",
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
    if send_tool_breakdown_flow_form(
        wa_id_to_phone(sender),
        employee_name=name,
        department=dept,
        jmd_route=(ud.get("jmd_route") or "").strip(),
    ):
        return
    logger.warning("tool breakdown flow template send failed sender=%s", sender)
    deps.send_to(
        sender,
        "Could not open Tool Breakdown form. Please try again or contact admin.",
    )


def handle_flow_submission(
    sender: str, response_json: dict | str | None, deps: ToolBreakdownDeps
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
            "Could not read the Tool Breakdown form. "
            "Ensure machine, tool name, issue, line stop threat, and photo are filled.",
        )
        return

    flow_data = _flow_data_dict(response_json)
    photo_raw = _issue_photo_from_flow_data(flow_data)
    from it_flow_media import photo_debug_summary, process_it_issue_photo

    ref = deps.db.collection("requests").document()
    request_id = ref.id
    now = deps.utcnow()
    reason = (
        f"{parsed['machine_no_label']} — {parsed['tool_name']} — "
        f"{parsed['issue_category_label']}"
    )

    logger.info(
        "tool breakdown flow submit request_id=%s photo=%s",
        request_id,
        photo_debug_summary(photo_raw),
    )
    if not photo_raw:
        deps.send_to(
            sender,
            "Photo is required. Please attach a photo and submit again.",
        )
        return

    photo_fields, status_msg = process_it_issue_photo(photo_raw, request_id)
    if not photo_fields or not (photo_fields.get("issue_photo_url") or "").strip():
        logger.warning(
            "tool breakdown photo upload failed request_id=%s status=%s",
            request_id,
            status_msg,
        )
        deps.send_to(sender, "Could not upload the photo. Please try again.")
        return

    repeated_count = count_repeated_breakdowns(
        deps.db,
        tool_name_normalized=parsed["tool_name_normalized"],
        issue_category=parsed["issue_category"],
        department=dept,
    )

    payload = {
        "request_id": request_id,
        "requested_datetime": now,
        "employee": sender,
        "employee_id": ud.get("employee_id") or "",
        "employee_name": ud.get("name") or "Employee",
        "department": dept,
        "jmd_route": route,
        "type": "TOOL_BREAKDOWN",
        "reason": reason,
        "machine_no": parsed["machine_no"],
        "machine_no_label": parsed["machine_no_label"],
        "tool_name": parsed["tool_name"],
        "tool_name_normalized": parsed["tool_name_normalized"],
        "issue_category": parsed["issue_category"],
        "issue_category_label": parsed["issue_category_label"],
        "line_stop_threat": parsed["line_stop_threat"],
        "line_stop_threat_label": parsed["line_stop_threat_label"],
        "repeated_count": repeated_count,
        "issue_photo_url": photo_fields.get("issue_photo_url") or "",
        "issue_photo_path": photo_fields.get("issue_photo_path") or "",
        "issue_photo_file_name": photo_fields.get("issue_photo_file_name") or "",
        "issue_photo_status": photo_fields.get("issue_photo_status") or "uploaded",
        "issue_photo_debug": status_msg,
        "tool_breakdown_status": "PENDING",
        "submission_source": "whatsapp_flow",
        "manager_status": "N/A",
        "jmd_status": "N/A",
        "md_status": "N/A",
        "source": "whatsapp_request",
    }
    ref.set(payload)

    _notify_tool_breakdown_manager(deps, payload, request_id)
    deps.send_to(sender, _employee_confirmation(payload))
    logger.info(
        "tool breakdown submitted request_id=%s employee=%s repeated=%s",
        request_id,
        sender,
        repeated_count,
    )
