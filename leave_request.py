"""
Leave request flow — when (today / tomorrow / other), dates, reason; JMD approval only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]

from bot_shared import (
    expand_leave_date_range,
    find_open_request,
    get_employee_leave_counts,
    get_user_record,
)
from interakt_api import send_reply_buttons, wa_id_to_phone

logger = logging.getLogger(__name__)

LEAVE_ALREADY_PENDING_MSG = "Your leave request is already pending approval."

LEAVE_SESSION_STATES = frozenset({
    "WAITING_LEAVE_WHEN",
    "WAITING_LEAVE_FROM_DATE",
    "WAITING_LEAVE_TO_DATE",
    "WAITING_LEAVE_REASON_PICK",
    "WAITING_LEAVE_REASON_TYPING",
})

WHEN_CHOICES = frozenset({"TODAY", "TOMORROW", "OTHER"})
REASON_CHOICES = frozenset({"SICK_LEAVE", "CASUAL_LEAVE", "LEAVE_REASON_OTHER"})

REASON_LABELS = {
    "SICK_LEAVE": "Sick Leave",
    "CASUAL_LEAVE": "Casual Leave",
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
    already_pending_msg: str = LEAVE_ALREADY_PENDING_MSG


def is_leave_state(state: str | None) -> bool:
    return (state or "") in LEAVE_SESSION_STATES


def try_start(sender: str, deps: LeaveDeps) -> None:
    if find_open_request(sender, "LEAVE"):
        deps.send_to(sender, deps.already_pending_msg)
        return
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


def _leave_days(from_s: str, to_s: str) -> int:
    f = _parse_ddmmy(from_s)
    t = _parse_ddmmy(to_s)
    if not f or not t:
        return 1
    return max(1, (t - f).days + 1)


def _go_to_reason_pick(
    sender: str,
    session: dict,
    deps: LeaveDeps,
    from_d: str,
    to_d: str,
) -> None:
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
    if find_open_request(sender, "LEAVE"):
        deps.session_ref(sender).delete()
        deps.send_to(sender, deps.already_pending_msg)
        return

    exists, ud = get_user_record(sender)
    if not exists or not ud:
        deps.session_ref(sender).delete()
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return

    chain = deps.build_approval_chain(ud)
    if not chain or not chain.get("jmd"):
        deps.session_ref(sender).delete()
        deps.send_to(
            sender,
            "Leave approver not configured.\n"
            "Set TEST_MD_WHATSAPP_NUMBER for testing, or contact admin.",
        )
        return

    days = _leave_days(from_d, to_d)
    leave_dates = expand_leave_date_range(from_d, to_d)
    employee_id = ud.get("employee_id") or ""
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
        "leave_days": days,
        "leave_dates": leave_dates,
        "leaves_last_month": leaves_last_month,
        "leaves_current_month": leaves_current_month,
        "jmd": chain["jmd"],
        "jmd_route": chain["jmd_route"],
        "md": chain.get("md") or "",
        "leave_test_approver": bool(chain.get("leave_test_approver")),
        "manager_status": "N/A",
        "jmd_status": "PENDING",
        "md_status": "N/A",
        "source": "whatsapp_request",
    })
    test_note = " (test approver)" if chain.get("leave_test_approver") else ""
    logger.info(
        "LEAVE created %s jmd_route=%s days=%s%s",
        request_id,
        chain["jmd_route"],
        days,
        test_note,
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
