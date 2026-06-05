"""
Permission request flow — reason for today (IST), JMD approval only.
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
    find_overlapping_permission_request,
    get_employee_permission_counts,
    get_user_record,
)
from interakt_api import send_reply_buttons, wa_id_to_phone

logger = logging.getLogger(__name__)

PERMISSION_SESSION_STATES = frozenset({
    "WAITING_PERMISSION_REASON",
    "WAITING_PERMISSION_CANCEL_CHOICE",
})

CANCEL_CHOICES = frozenset({"PERMISSION_CANCEL", "PERMISSION_EXIT"})


@dataclass
class PermissionDeps:
    db: object
    send_to: Callable[[str, str], None]
    session_merge: Callable[..., None]
    session_ref: Callable[[str], object]
    utcnow: Callable
    chat_name: Callable[[str], str]
    build_approval_chain: Callable[[dict], dict | None]
    notify_jmd: Callable[[str, dict, str], bool]
    go_main_menu: Callable[[str], None]


def is_permission_state(state: str | None) -> bool:
    return (state or "") in PERMISSION_SESSION_STATES


def _ist_tzinfo():
    if ZoneInfo:
        return ZoneInfo("Asia/Kolkata")
    return timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> date:
    return datetime.now(_ist_tzinfo()).date()


def _today_ddmmy() -> str:
    return _today_ist().strftime("%d-%m-%Y")


def try_start(sender: str, deps: PermissionDeps) -> None:
    exists, ud = get_user_record(sender)
    if not exists or not ud:
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return

    session = {
        "employee_name": ud.get("name") or "Employee",
        "department": ud.get("department") or "",
    }
    if _check_permission_overlap(sender, session, deps):
        return

    deps.session_merge(
        sender,
        state="WAITING_PERMISSION_REASON",
        employee_name=session["employee_name"],
        department=session["department"],
        permission_date=_today_ddmmy(),
        form_type="PERMISSION_REQUEST",
    )
    deps.send_to(sender, "Please type your permission reason:")


def handle(sender: str, incoming: str, session: dict, deps: PermissionDeps) -> None:
    state = session.get("state")

    if state == "WAITING_PERMISSION_REASON":
        reason = (incoming or "").strip()
        if not reason:
            deps.send_to(sender, "Please type your permission reason:")
            return
        if _normalize_incoming(incoming) in CANCEL_CHOICES:
            deps.send_to(sender, "Please type your permission reason:")
            return
        _submit(sender, session, deps, reason=reason[:500])
        return

    if state == "WAITING_PERMISSION_CANCEL_CHOICE":
        cancel_choice = _normalize_cancel_choice(incoming)
        if cancel_choice not in CANCEL_CHOICES:
            _send_overlap_cancel_buttons(
                sender,
                deps,
                _overlap_cancel_body(session.get("permission_overlap_status") or ""),
            )
            return
        if cancel_choice == "PERMISSION_EXIT":
            deps.session_ref(sender).delete()
            deps.send_to(sender, "Okay.")
            return
        req_id = (session.get("permission_overlap_request_id") or "").strip()
        ok, err = _employee_cancel_permission(sender, req_id, deps)
        deps.session_ref(sender).delete()
        if ok:
            deps.send_to(sender, "Your permission request has been cancelled.")
        else:
            deps.send_to(
                sender, err or "Could not cancel permission. Please contact admin."
            )
        return

    deps.send_to(sender, "Invalid step. Send Hi to start over.")


def _overlap_cancel_body(overlap_status: str) -> str:
    if overlap_status == "approved":
        status_line = (
            "A permission request for today is already raised and is approved."
        )
    else:
        status_line = (
            "A permission request for today is already raised and is pending approval."
        )
    return f"{status_line}\n\nDo you want to cancel permission?"


def _prompt_permission_cancel_or_exit(
    sender: str,
    session: dict,
    deps: PermissionDeps,
    overlap_doc: dict,
    overlap_status: str,
) -> None:
    req_id = (overlap_doc.get("request_id") or "").strip()
    deps.session_merge(
        sender,
        state="WAITING_PERMISSION_CANCEL_CHOICE",
        permission_overlap_request_id=req_id,
        permission_overlap_status=overlap_status,
        permission_date=_today_ddmmy(),
        employee_name=session.get("employee_name"),
        department=session.get("department"),
        form_type="PERMISSION_REQUEST",
    )
    _send_overlap_cancel_buttons(sender, deps, _overlap_cancel_body(overlap_status))


def _check_permission_overlap(
    sender: str, session: dict, deps: PermissionDeps
) -> bool:
    exists, ud = get_user_record(sender)
    if not exists or not ud:
        return False
    overlap_doc, overlap_status = find_overlapping_permission_request(
        deps.db,
        ud.get("employee_id") or "",
        _today_ddmmy(),
        employee_wa=sender,
    )
    if not overlap_status or not overlap_doc:
        return False
    _prompt_permission_cancel_or_exit(
        sender, session, deps, overlap_doc, overlap_status
    )
    return True


def _send_overlap_cancel_buttons(wa_id: str, deps: PermissionDeps, body: str) -> None:
    try:
        send_reply_buttons(
            wa_id_to_phone(wa_id),
            body,
            [
                ("PERMISSION_CANCEL", "Cancel Permission"),
                ("PERMISSION_EXIT", "Exit"),
            ],
            callback_data="permission-overlap",
        )
    except Exception:
        logger.exception("permission overlap buttons failed")
        deps.send_to(wa_id, f"{body}\n\nReply: Cancel Permission or Exit.")


def _employee_cancel_permission(
    sender: str, request_id: str, deps: PermissionDeps
) -> tuple[bool, str | None]:
    rid = (request_id or "").strip()
    if not rid:
        return False, "Permission request not found."
    exists, ud = get_user_record(sender)
    if not exists or not ud:
        return False, "User not registered.\nPlease contact admin."
    employee_id = (ud.get("employee_id") or "").strip().upper()
    try:
        ref = deps.db.collection("requests").document(rid)
        snap = ref.get()
        if not snap.exists:
            return False, "Permission request not found."
        d = snap.to_dict() or {}
        if (d.get("type") or "").strip().upper() != "PERMISSION":
            return False, "Not a permission request."
        owner_wa = (d.get("employee") or "").strip()
        owner_id = (d.get("employee_id") or "").strip().upper()
        if owner_wa != sender and owner_id != employee_id:
            return False, "You can only cancel your own permission request."
        jmd = (d.get("jmd_status") or "").strip().upper()
        if jmd in ("CANCELLED", "DENIED") or d.get("cancelled_by_employee"):
            return False, "This permission request is already cancelled."
        ref.update({
            "jmd_status": "CANCELLED",
            "cancelled_by_employee": True,
            "cancelled_at": deps.utcnow(),
        })
        logger.info("PERMISSION cancelled by employee %s request_id=%s", sender, rid)
        return True, None
    except Exception:
        logger.exception("employee permission cancel failed request_id=%s", rid)
        return False, "Could not cancel permission. Please try again or contact admin."


def _submit(sender: str, session: dict, deps: PermissionDeps, *, reason: str) -> None:
    exists, ud = get_user_record(sender)
    if not exists or not ud:
        deps.session_ref(sender).delete()
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return

    if _check_permission_overlap(sender, session, deps):
        return

    employee_id = ud.get("employee_id") or ""
    permission_date = _today_ddmmy()
    chain = deps.build_approval_chain(ud)
    if not chain or not chain.get("jmd"):
        deps.session_ref(sender).delete()
        deps.send_to(
            sender,
            "Permission approver not configured.\n"
            "Set TEST_MD_WHATSAPP_NUMBER for testing, or contact admin.",
        )
        return

    perms_last_month, perms_current_month = get_employee_permission_counts(
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
        "type": "PERMISSION",
        "reason": reason,
        "permission_date": permission_date,
        "permissions_last_month": perms_last_month,
        "permissions_current_month": perms_current_month,
        "jmd": chain["jmd"],
        "jmd_route": chain["jmd_route"],
        "md": chain.get("md") or "",
        "permission_test_approver": bool(chain.get("permission_test_approver")),
        "manager_status": "N/A",
        "jmd_status": "PENDING",
        "md_status": "N/A",
        "source": "whatsapp_request",
    })
    test_note = " (test approver)" if chain.get("permission_test_approver") else ""
    logger.info(
        "PERMISSION created %s jmd_route=%s date=%s%s",
        request_id,
        chain["jmd_route"],
        permission_date,
        test_note,
    )

    rd = ref.get().to_dict()
    jmd_ok = deps.notify_jmd(chain["jmd"], rd, request_id)

    deps.session_ref(sender).delete()
    msg = "Your permission request has been submitted for approval."
    if not jmd_ok:
        route = chain["jmd_route"]
        msg += (
            f"\n\nJMD ({route}) could not be notified on WhatsApp. "
            "Ask them to send Hi to this Alubee number once, then contact admin."
        )
    deps.send_to(sender, msg)


def _normalize_incoming(incoming: str) -> str:
    return (incoming or "").strip().upper().replace(" ", "_")


def _normalize_cancel_choice(incoming: str) -> str:
    c = _normalize_incoming(incoming)
    if c in ("PERMISSION_CANCEL", "CANCEL_PERMISSION", "CANCEL"):
        return "PERMISSION_CANCEL"
    if c in ("PERMISSION_EXIT", "EXIT"):
        return "PERMISSION_EXIT"
    low = (incoming or "").strip().lower()
    if low in ("cancel permission", "cancel"):
        return "PERMISSION_CANCEL"
    if low == "exit":
        return "PERMISSION_EXIT"
    return c
