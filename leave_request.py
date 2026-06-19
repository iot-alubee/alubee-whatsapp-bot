"""
Leave request flow — when (today / tomorrow / other), dates, reason; JMD → MD approval.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]

from bot_shared import (
    expand_leave_date_range,
    find_overlapping_leave_request,
    get_employee_leave_counts,
    get_user_record,
)
from interakt_api import send_reply_buttons, wa_id_to_phone

logger = logging.getLogger(__name__)

LEAVE_SESSION_STATES = frozenset({
    "WAITING_LEAVE_WHEN",
    "WAITING_LEAVE_FROM_DATE",
    "WAITING_LEAVE_TO_DATE",
    "WAITING_LEAVE_REASON_PICK",
    "WAITING_LEAVE_REASON_TYPING",
    "WAITING_LEAVE_CANCEL_CHOICE",
})

CANCEL_CHOICES = frozenset({"LEAVE_CANCEL", "LEAVE_EXIT"})

WHEN_CHOICES = frozenset({"TODAY", "TOMORROW", "OTHER"})
REASON_CHOICES = frozenset({"SICK_LEAVE", "CASUAL_LEAVE", "LEAVE_REASON_OTHER"})

REASON_LABELS = {
    "SICK_LEAVE": "Sick Leave",
    "CASUAL_LEAVE": "Casual Leave",
    "HEALTH_ISSUE": "Health Issue",
    "PERSONAL": "Personal",
}


@dataclass
class LeaveDeps:
    db: object
    send_to: Callable[[str, str], None]
    session_merge: Callable[..., None]
    session_ref: Callable[[str], object]
    utcnow: Callable
    chat_name: Callable[[str], str]
    build_approval_chain: Callable[[dict], dict | None]
    notify_jmd: Callable[[str, dict, str], bool]
    go_main_menu: Callable[[str], None]


def is_leave_state(state: str | None) -> bool:
    return (state or "") in LEAVE_SESSION_STATES


def leave_flow_template_name() -> str:
    return (os.getenv("LEAVE_FLOW_TEMPLATE_NAME") or "").strip()


def leave_flow_enabled() -> bool:
    return bool(leave_flow_template_name())


def try_start_form(sender: str, deps: LeaveDeps) -> None:
    """Send WhatsApp Flow form (menu: Leave - Form). Chat leave flow unchanged."""
    exists, ud = get_user_record(sender)
    if not exists or not ud:
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return
    if not leave_flow_enabled():
        deps.send_to(
            sender,
            "Leave form is not configured yet.\n"
            "Use Leave Request (chat) or contact admin.",
        )
        return
    from interakt_api import send_leave_flow_form

    name = ud.get("name") or "Employee"
    if send_leave_flow_form(wa_id_to_phone(sender), employee_name=name):
        return
    logger.warning("leave flow template send failed sender=%s", sender)
    deps.send_to(
        sender,
        "Could not open leave form. Try Leave Request (chat) or contact admin.",
    )


def try_start(sender: str, deps: LeaveDeps) -> None:
    exists, ud = get_user_record(sender)
    if not exists or not ud:
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return
    deps.session_merge(
        sender,
        state="WAITING_LEAVE_WHEN",
        employee_name=ud.get("name") or "Employee",
        department=ud.get("department") or "",
        form_type="LEAVE_REQUEST",
    )
    _send_when_buttons(sender, deps)


def handle(sender: str, incoming: str, session: dict, deps: LeaveDeps) -> None:
    state = session.get("state")
    choice = _normalize_incoming(incoming)

    if state == "WAITING_LEAVE_WHEN":
        when = _normalize_when_choice(incoming)
        if when not in WHEN_CHOICES:
            deps.send_to(sender, "Choose Today, Tomorrow, or Other.")
            return
        if when == "OTHER":
            deps.session_merge(sender, state="WAITING_LEAVE_FROM_DATE")
            deps.send_to(sender, "From date (DD-MM-YYYY):")
            return
        from_d, to_d = _dates_for_when(when)
        _go_to_reason_pick(sender, session, deps, from_d, to_d)
        return

    if state == "WAITING_LEAVE_FROM_DATE":
        from_d, err = _parse_leave_date(incoming)
        if not from_d:
            deps.send_to(sender, err or "From date (DD-MM-YYYY):")
            return
        deps.session_merge(
            sender,
            state="WAITING_LEAVE_TO_DATE",
            leave_from_date=from_d,
        )
        deps.send_to(sender, "To date (DD-MM-YYYY):")
        return

    if state == "WAITING_LEAVE_TO_DATE":
        from_d = (session.get("leave_from_date") or "").strip()
        to_d, err = _parse_leave_date(incoming, min_date=_parse_ddmmy(from_d))
        if not to_d:
            deps.send_to(sender, err or "To date (DD-MM-YYYY):")
            return
        if _parse_ddmmy(from_d) and _parse_ddmmy(to_d) < _parse_ddmmy(from_d):
            deps.send_to(
                sender,
                "To date cannot be before From date.\nEnter To date (DD-MM-YYYY):",
            )
            return
        _go_to_reason_pick(sender, session, deps, from_d, to_d)
        return

    if state == "WAITING_LEAVE_REASON_PICK":
        reason_code = _normalize_reason_choice(incoming)
        if reason_code not in REASON_CHOICES:
            deps.send_to(sender, "Choose Sick Leave, Casual Leave, or Other.")
            return
        if reason_code == "LEAVE_REASON_OTHER":
            deps.session_merge(sender, state="WAITING_LEAVE_REASON_TYPING")
            deps.send_to(sender, "Please type your leave reason:")
            return
        label = REASON_LABELS[reason_code]
        _submit_from_session(
            sender,
            {**session, "leave_reason_code": reason_code, "leave_reason": label},
            deps,
        )
        return

    if state == "WAITING_LEAVE_REASON_TYPING":
        if choice in REASON_CHOICES or choice in WHEN_CHOICES:
            deps.send_to(sender, "Please type your leave reason (text).")
            return
        reason_text = (incoming or "").strip()
        if not reason_text:
            deps.send_to(sender, "Please type your leave reason:")
            return
        _submit_from_session(
            sender,
            {
                **session,
                "leave_reason_code": "OTHER",
                "leave_reason": reason_text[:500],
            },
            deps,
        )
        return

    if state == "WAITING_LEAVE_CANCEL_CHOICE":
        cancel_choice = _normalize_cancel_choice(incoming)
        if cancel_choice not in CANCEL_CHOICES:
            _send_overlap_cancel_buttons(
                sender,
                deps,
                _overlap_cancel_body(session.get("leave_overlap_status") or ""),
            )
            return
        if cancel_choice == "LEAVE_EXIT":
            deps.session_ref(sender).delete()
            deps.send_to(sender, "Okay.")
            return
        req_id = (session.get("leave_overlap_request_id") or "").strip()
        ok, err = _employee_cancel_leave(sender, req_id, deps)
        deps.session_ref(sender).delete()
        if ok:
            deps.send_to(sender, "Your leave request has been cancelled.")
        else:
            deps.send_to(sender, err or "Could not cancel leave. Please contact admin.")
        return

    deps.send_to(sender, "Invalid step. Send Hi to start over.")


def _ist_tzinfo():
    if ZoneInfo:
        return ZoneInfo("Asia/Kolkata")
    return timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> date:
    return datetime.now(_ist_tzinfo()).date()


def _format_ddmmy(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _parse_ddmmy(text: str) -> date | None:
    raw = (text or "").strip()
    if not raw:
        return None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    if len(raw) >= 10:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def _parse_leave_date(
    text: str, *, min_date: date | None = None
) -> tuple[str | None, str]:
    raw = (text or "").strip()
    if not raw:
        return None, f"Enter date as DD-MM-YYYY (e.g. {_format_ddmmy(_today_ist())})."

    parsed = _parse_ddmmy(raw)
    if parsed is None:
        return None, (
            "Date format is not correct.\n"
            "Enter again as DD-MM-YYYY (e.g. 30-05-2026)."
        )

    today = _today_ist()
    if parsed.year != today.year:
        return None, (
            f"Date must be in {today.year}.\n"
            "Enter again as DD-MM-YYYY (today or a future date)."
        )
    if parsed < today:
        return None, (
            "Date cannot be in the past.\n"
            f"Enter today ({_format_ddmmy(today)}) or a future date (DD-MM-YYYY)."
        )
    if min_date and parsed < min_date:
        return None, (
            f"Date cannot be before {_format_ddmmy(min_date)}.\n"
            "Enter again as DD-MM-YYYY."
        )
    return _format_ddmmy(parsed), ""


def _dates_for_when(when: str) -> tuple[str, str]:
    today = _today_ist()
    if when == "TOMORROW":
        d = today + timedelta(days=1)
    else:
        d = today
    s = _format_ddmmy(d)
    return s, s


def _normalize_leave_duration(raw: str) -> str:
    d = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if d in ("half_day", "half", "halfday", "0.5"):
        return "half_day"
    return "full_day"


def _leave_days(
    from_s: str, to_s: str, *, leave_duration: str = "full_day"
) -> float:
    f = _parse_ddmmy(from_s)
    t = _parse_ddmmy(to_s)
    if not f or not t:
        calendar = 1
    else:
        calendar = max(1, (t - f).days + 1)
    if _normalize_leave_duration(leave_duration) == "half_day" and calendar == 1:
        return 0.5
    return float(calendar)


def format_leave_days_display(days, leave_duration: str = "") -> str:
    """Human-readable day count for approval UI and messages."""
    dur = _normalize_leave_duration(leave_duration)
    try:
        value = float(days)
    except (TypeError, ValueError):
        value = 1.0
    if dur == "half_day" or value == 0.5:
        return "0.5"
    if value == int(value):
        return str(int(value))
    return str(value)


def current_leave_days_num(rd: dict) -> float:
    try:
        days = float(rd.get("leave_days") or 1)
    except (TypeError, ValueError):
        days = 1.0
    from_d = (rd.get("leave_from_date") or "").strip()
    to_d = (rd.get("leave_to_date") or from_d).strip()
    if days <= 1 and from_d and to_d and from_d != to_d:
        return _leave_days(from_d, to_d, leave_duration=rd.get("leave_duration") or "")
    return days


def leave_manage_eligible(rd: dict) -> bool:
    """True when JMD/MD may reduce a multi-day leave before approval."""
    if (rd.get("type") or "").strip().upper() != "LEAVE":
        return False
    if _normalize_leave_duration(rd.get("leave_duration") or "") == "half_day":
        return False
    return current_leave_days_num(rd) > 1


def parse_reduced_leave_days(raw: str, *, current_days: float) -> tuple[int | None, str]:
    text = (raw or "").strip().lower()
    if text in ("cancel", "back", "exit"):
        return None, "cancel"
    if not text.isdigit():
        max_days = int(current_days) - 1
        return None, (
            f"Reply with a number from 1 to {max_days} to reduce leave days, "
            "or CANCEL to go back."
        )
    value = int(text)
    max_days = int(current_days) - 1
    if value < 1 or value > max_days:
        return None, f"Enter a number from 1 to {max_days}, or CANCEL to go back."
    return value, ""


def apply_leave_reduction(from_s: str, new_days: int) -> dict:
    n = max(1, int(new_days))
    start = _parse_ddmmy(from_s)
    if not start:
        to_s = from_s
    else:
        to_s = _format_ddmmy(start + timedelta(days=n - 1))
    dates = expand_leave_date_range(from_s, to_s)
    return {
        "leave_days": float(n),
        "leave_to_date": to_s,
        "leave_dates": dates,
        "leave_duration": "full_day",
    }


def employee_leave_approved_message(rd: dict) -> str:
    days = format_leave_days_display(
        rd.get("leave_days"),
        rd.get("leave_duration") or "",
    )
    original = rd.get("leave_days_original")
    if original is not None:
        try:
            if float(original) != float(rd.get("leave_days") or 0):
                orig_disp = format_leave_days_display(original, "")
                return (
                    f"Your leave request has been approved for {days} day(s) "
                    f"(reduced from {orig_disp})."
                )
        except (TypeError, ValueError):
            pass
    return f"Your leave request has been approved for {days} day(s)."


def _overlap_cancel_body(overlap_status: str) -> str:
    if overlap_status == "approved":
        status_line = (
            "A leave request for this date is already raised and is approved."
        )
    else:
        status_line = (
            "A leave request for this date is already raised and is pending approval."
        )
    return f"{status_line}\n\nDo you want to cancel leave?"


def _prompt_leave_cancel_or_exit(
    sender: str,
    session: dict,
    deps: LeaveDeps,
    overlap_doc: dict,
    overlap_status: str,
    from_d: str,
    to_d: str,
) -> None:
    req_id = (overlap_doc.get("request_id") or "").strip()
    deps.session_merge(
        sender,
        state="WAITING_LEAVE_CANCEL_CHOICE",
        leave_overlap_request_id=req_id,
        leave_overlap_status=overlap_status,
        leave_from_date=from_d,
        leave_to_date=to_d,
        employee_name=session.get("employee_name"),
        department=session.get("department"),
        form_type="LEAVE_REQUEST",
    )
    _send_overlap_cancel_buttons(sender, deps, _overlap_cancel_body(overlap_status))


def _check_leave_overlap(
    sender: str,
    session: dict,
    deps: LeaveDeps,
    from_d: str,
    to_d: str,
) -> bool:
    """True if overlap was found and cancel/exit prompt was sent."""
    exists, ud = get_user_record(sender)
    if not exists or not ud:
        return False
    overlap_doc, overlap_status = find_overlapping_leave_request(
        deps.db,
        ud.get("employee_id") or "",
        from_d,
        to_d,
        employee_wa=sender,
    )
    if not overlap_status or not overlap_doc:
        return False
    _prompt_leave_cancel_or_exit(
        sender, session, deps, overlap_doc, overlap_status, from_d, to_d
    )
    return True


def _go_to_reason_pick(
    sender: str,
    session: dict,
    deps: LeaveDeps,
    from_d: str,
    to_d: str,
) -> None:
    if _check_leave_overlap(sender, session, deps, from_d, to_d):
        return
    deps.session_merge(
        sender,
        state="WAITING_LEAVE_REASON_PICK",
        leave_from_date=from_d,
        leave_to_date=to_d,
        leave_days=_leave_days(from_d, to_d),
        employee_name=session.get("employee_name"),
        department=session.get("department"),
    )
    _send_reason_buttons(sender, deps)


def _send_when_buttons(wa_id: str, deps: LeaveDeps) -> None:
    try:
        send_reply_buttons(
            wa_id_to_phone(wa_id),
            "When is your leave?",
            [
                ("LEAVE_TODAY", "Today"),
                ("LEAVE_TOMORROW", "Tomorrow"),
                ("LEAVE_OTHER", "Other"),
            ],
            callback_data="leave-when",
        )
    except Exception:
        logger.exception("leave when buttons failed")
        deps.send_to(wa_id, "When is your leave?\nReply: Today, Tomorrow, or Other.")


def _send_overlap_cancel_buttons(wa_id: str, deps: LeaveDeps, body: str) -> None:
    try:
        send_reply_buttons(
            wa_id_to_phone(wa_id),
            body,
            [
                ("LEAVE_CANCEL", "Cancel Leave"),
                ("LEAVE_EXIT", "Exit"),
            ],
            callback_data="leave-overlap",
        )
    except Exception:
        logger.exception("leave overlap buttons failed")
        deps.send_to(
            wa_id,
            f"{body}\n\nReply: Cancel Leave or Exit.",
        )


def _employee_cancel_leave(
    sender: str, request_id: str, deps: LeaveDeps
) -> tuple[bool, str | None]:
    rid = (request_id or "").strip()
    if not rid:
        return False, "Leave request not found."
    exists, ud = get_user_record(sender)
    if not exists or not ud:
        return False, "User not registered.\nPlease contact admin."
    employee_id = (ud.get("employee_id") or "").strip().upper()
    try:
        ref = deps.db.collection("requests").document(rid)
        snap = ref.get()
        if not snap.exists:
            return False, "Leave request not found."
        d = snap.to_dict() or {}
        if (d.get("type") or "").strip().upper() != "LEAVE":
            return False, "Not a leave request."
        if (d.get("source") or "").strip().lower() == "imported_history":
            return False, "This leave record cannot be cancelled here."
        owner_wa = (d.get("employee") or "").strip()
        owner_id = (d.get("employee_id") or "").strip().upper()
        if owner_wa != sender and owner_id != employee_id:
            return False, "You can only cancel your own leave request."
        jmd = (d.get("jmd_status") or "").strip().upper()
        if jmd in ("CANCELLED", "DENIED") or d.get("cancelled_by_employee"):
            return False, "This leave request is already cancelled."
        ref.update({
            "jmd_status": "CANCELLED",
            "cancelled_by_employee": True,
            "cancelled_at": deps.utcnow(),
        })
        logger.info("LEAVE cancelled by employee %s request_id=%s", sender, rid)
        return True, None
    except Exception:
        logger.exception("employee leave cancel failed request_id=%s", rid)
        return False, "Could not cancel leave. Please try again or contact admin."


def _send_reason_buttons(wa_id: str, deps: LeaveDeps) -> None:
    try:
        send_reply_buttons(
            wa_id_to_phone(wa_id),
            "Leave reason:",
            [
                ("SICK_LEAVE", "Sick Leave"),
                ("CASUAL_LEAVE", "Casual Leave"),
                ("LEAVE_REASON_OTHER", "Other"),
            ],
            callback_data="leave-reason",
        )
    except Exception:
        logger.exception("leave reason buttons failed")
        deps.send_to(
            wa_id,
            "Leave reason:\nChoose Sick Leave, Casual Leave, or Other.",
        )


def _submit_from_session(sender: str, session: dict, deps: LeaveDeps) -> None:
    reason = (session.get("leave_reason") or "").strip()
    from_d = (session.get("leave_from_date") or "").strip()
    to_d = (session.get("leave_to_date") or from_d).strip()
    if not reason or not from_d:
        deps.send_to(sender, "Missing leave details. Send Hi to start again.")
        return
    _submit(sender, session, deps, reason=reason, from_d=from_d, to_d=to_d)


def _submit(
    sender: str,
    session: dict,
    deps: LeaveDeps,
    *,
    reason: str,
    from_d: str,
    to_d: str,
) -> None:
    exists, ud = get_user_record(sender)
    if not exists or not ud:
        deps.session_ref(sender).delete()
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return

    if _check_leave_overlap(sender, session, deps, from_d, to_d):
        return

    employee_id = ud.get("employee_id") or ""
    chain = deps.build_approval_chain(ud)
    if not chain or not chain.get("jmd") or not chain.get("md"):
        deps.session_ref(sender).delete()
        deps.send_to(
            sender,
            "Leave approvers not configured.\nPlease contact admin.",
        )
        return

    leave_duration = _normalize_leave_duration(session.get("leave_duration") or "full_day")
    days = session.get("leave_days")
    if days is None:
        days = _leave_days(from_d, to_d, leave_duration=leave_duration)
    else:
        try:
            days = float(days)
        except (TypeError, ValueError):
            days = _leave_days(from_d, to_d, leave_duration=leave_duration)
    leave_dates = expand_leave_date_range(from_d, to_d)
    leaves_last_month, leaves_current_month = get_employee_leave_counts(
        employee_id,
        employee_wa=sender,
        firestore_db=deps.db,
    )
    ref = deps.db.collection("requests").document()
    request_id = ref.id
    ref.set({
        "request_id": request_id,
        "requested_datetime": deps.utcnow(),
        "employee": sender,
        "employee_id": employee_id,
        "employee_name": ud.get("name") or session.get("employee_name") or "Employee",
        "department": ud.get("department") or session.get("department") or "",
        "type": "LEAVE",
        "reason": reason,
        "leave_reason_code": session.get("leave_reason_code") or "",
        "leave_from_date": from_d,
        "leave_to_date": to_d,
        "leave_duration": leave_duration,
        "leave_days": days,
        "leave_dates": leave_dates,
        "leaves_last_month": leaves_last_month,
        "leaves_current_month": leaves_current_month,
        "jmd": chain["jmd"],
        "jmd_route": chain["jmd_route"],
        "md": chain["md"],
        "manager_status": "N/A",
        "jmd_status": "PENDING",
        "md_status": "AWAITING_JMD",
        "source": "whatsapp_request",
    })
    logger.info(
        "LEAVE created %s jmd_route=%s days=%s",
        request_id,
        chain["jmd_route"],
        days,
    )

    rd = ref.get().to_dict()
    jmd_ok = deps.notify_jmd(chain["jmd"], rd, request_id)

    deps.session_ref(sender).delete()
    msg = "Your leave request has been submitted for approval."
    if not jmd_ok:
        route = chain["jmd_route"]
        msg += (
            f"\n\nJMD ({route}) could not be notified on WhatsApp. "
            "Ask them to send Hi to this Alubee number once, then contact admin."
        )
    deps.send_to(sender, msg)


def _normalize_incoming(incoming: str) -> str:
    return (incoming or "").strip().upper().replace(" ", "_")


def _normalize_when_choice(incoming: str) -> str:
    c = _normalize_incoming(incoming)
    if c in ("LEAVE_TODAY", "TODAY", "1"):
        return "TODAY"
    if c in ("LEAVE_TOMORROW", "TOMORROW", "2"):
        return "TOMORROW"
    if c in ("LEAVE_OTHER", "OTHER", "3"):
        return "OTHER"
    low = (incoming or "").strip().lower()
    if low == "today":
        return "TODAY"
    if low == "tomorrow":
        return "TOMORROW"
    if low == "other":
        return "OTHER"
    return c


def _normalize_cancel_choice(incoming: str) -> str:
    c = _normalize_incoming(incoming)
    if c in ("LEAVE_CANCEL", "CANCEL_LEAVE", "CANCEL"):
        return "LEAVE_CANCEL"
    if c in ("LEAVE_EXIT", "EXIT"):
        return "LEAVE_EXIT"
    low = (incoming or "").strip().lower()
    if low in ("cancel leave", "cancel"):
        return "LEAVE_CANCEL"
    if low == "exit":
        return "LEAVE_EXIT"
    return c


def _flow_pick(data: dict, *needles: str) -> str:
    if not data:
        return ""
    for key in needles:
        if key in data and data[key] not in (None, ""):
            return str(data[key]).strip()
    for raw_key, val in data.items():
        if val in (None, ""):
            continue
        lk = str(raw_key).lower()
        for needle in needles:
            if needle.lower() in lk:
                return str(val).strip()
    return ""


def _normalize_flow_date(raw: str) -> str:
    parsed, _ = _parse_leave_date(raw)
    return parsed or ""


def parse_flow_response(response_json: dict | str | None) -> dict | None:
    """Map WhatsApp Flow submit payload to leave submit kwargs."""
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

    when_raw = _flow_pick(data, "leave_when", "when").lower().replace(" ", "_")
    if when_raw in ("today", "leave_today", "1"):
        when = "TODAY"
    elif when_raw in ("tomorrow", "leave_tomorrow", "2"):
        when = "TOMORROW"
    elif when_raw in ("other", "leave_other", "3"):
        when = "OTHER"
    else:
        return None

    today = _today_ist()
    tomorrow = today + timedelta(days=1)

    if when == "OTHER":
        from_d = _normalize_flow_date(_flow_pick(data, "from_date", "leave_from_date"))
        to_d = _normalize_flow_date(_flow_pick(data, "to_date", "leave_to_date"))
        if not from_d or not to_d:
            return None
        from_dt = _parse_ddmmy(from_d)
        to_dt = _parse_ddmmy(to_d)
        if not from_dt or not to_dt or to_dt < from_dt:
            return None
        if from_dt < tomorrow or to_dt < tomorrow:
            return None
        leave_duration = "full_day"
    elif when == "TOMORROW":
        from_d, to_d = _dates_for_when(when)
        leave_duration = _normalize_leave_duration(
            _flow_pick(data, "leave_duration", "duration")
        )
    else:
        from_d, to_d = _dates_for_when(when)
        leave_duration = "full_day"

    reason_raw = _flow_pick(data, "leave_reason", "reason").lower().replace(" ", "_")
    other_reason = _flow_pick(data, "other_reason", "reason_text")
    if reason_raw in ("health_issue", "health"):
        reason_code = "HEALTH_ISSUE"
        reason = REASON_LABELS[reason_code]
    elif reason_raw == "personal":
        reason_code = "PERSONAL"
        reason = REASON_LABELS[reason_code]
    elif reason_raw in ("sick_leave", "sick"):
        reason_code = "SICK_LEAVE"
        reason = REASON_LABELS[reason_code]
    elif reason_raw in ("casual_leave", "casual"):
        reason_code = "CASUAL_LEAVE"
        reason = REASON_LABELS[reason_code]
    elif reason_raw == "other":
        if not other_reason:
            return None
        reason_code = "OTHER"
        reason = other_reason[:500]
    else:
        return None

    if leave_duration == "half_day" and when != "TOMORROW":
        return None

    return {
        "leave_from_date": from_d,
        "leave_to_date": to_d,
        "leave_duration": leave_duration,
        "leave_days": _leave_days(from_d, to_d, leave_duration=leave_duration),
        "leave_reason_code": reason_code,
        "leave_reason": reason,
    }


def handle_flow_submission(
    sender: str, response_json: dict | str | None, deps: LeaveDeps
) -> None:
    parsed = parse_flow_response(response_json)
    if not parsed:
        deps.send_to(
            sender,
            "Could not read the leave form. Leave cannot be raised for today's date "
            "when using Other dates. Please try again or contact admin.",
        )
        return
    exists, ud = get_user_record(sender)
    session = {
        "employee_name": (ud or {}).get("name") or "Employee",
        "department": (ud or {}).get("department") or "",
        **parsed,
    }
    _submit_from_session(sender, session, deps)


def _normalize_reason_choice(incoming: str) -> str:
    c = _normalize_incoming(incoming)
    if c in ("SICK_LEAVE", "SICK", "1"):
        return "SICK_LEAVE"
    if c in ("CASUAL_LEAVE", "CASUAL", "2"):
        return "CASUAL_LEAVE"
    if c in ("LEAVE_REASON_OTHER", "OTHER", "3"):
        return "LEAVE_REASON_OTHER"
    low = (incoming or "").strip().lower()
    if low in ("sick leave", "sick"):
        return "SICK_LEAVE"
    if low in ("casual leave", "casual"):
        return "CASUAL_LEAVE"
    if low == "other":
        return "LEAVE_REASON_OTHER"
    return c
