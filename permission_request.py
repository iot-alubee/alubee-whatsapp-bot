"""
Permission request flow — myself / CL (supervisor), shift, type, reason; JMD/MD or PPC/HR approval.
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
    find_overlapping_cl_permission_request,
    find_overlapping_permission_request,
    get_employee_permission_counts,
    get_user_record,
)
from interakt_api import send_reply_buttons, wa_id_to_phone

logger = logging.getLogger(__name__)

PERMISSION_SESSION_STATES = frozenset({
    "WAITING_PERMISSION_FOR",
    "WAITING_PERMISSION_SHIFT",
    "WAITING_PERMISSION_CL_NAME",
    "WAITING_PERMISSION_TYPE",
    "WAITING_PERMISSION_REASON",
    "WAITING_PERMISSION_CANCEL_CHOICE",
})

FOR_CHOICES = frozenset({"PERMISSION_FOR_MYSELF", "PERMISSION_FOR_CL"})
SHIFT_CHOICES = frozenset({"PERMISSION_SHIFT_I", "PERMISSION_SHIFT_II"})

TYPE_CHOICES = frozenset({
    "PERMISSION_LATE_IN",
    "PERMISSION_EARLY_OUT",
    "PERMISSION_OTHER",
})

TYPE_LABELS = {
    "PERMISSION_LATE_IN": "Late IN",
    "PERMISSION_EARLY_OUT": "Early OUT",
    "PERMISSION_OTHER": "Other",
}

CANCEL_CHOICES = frozenset({"PERMISSION_CANCEL", "PERMISSION_EXIT"})


@dataclass
class PermissionDeps:
    db: object
    send_to: Callable[[str, str], None]
    session_merge: Callable[..., None]
    session_ref: Callable[[str], object]
    utcnow: Callable
    chat_name: Callable[[str], str]
    build_approval_chain: Callable[..., dict | None]
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


def _compute_permission_work_date(
    ud: dict,
    *,
    permission_shift: str,
    permission_type_code: str,
    permission_for: str = "myself",
) -> str:
    """
    RS: between 00:00–08:00 IST, Shift II + Early OUT → previous calendar day.
    Otherwise work date = today (IST).
    """
    today = _today_ist()
    calendar = today.strftime("%d-%m-%Y")
    if not _is_rotational_shift(ud):
        return calendar

    shift = (permission_shift or "").strip().upper()
    if permission_for == "cl":
        type_code = "PERMISSION_EARLY_OUT"
    else:
        type_code = (permission_type_code or "").strip().upper()

    now = datetime.now(_ist_tzinfo())
    if (
        shift in ("II", "2")
        and type_code == "PERMISSION_EARLY_OUT"
        and now.hour < 8
    ):
        return (today - timedelta(days=1)).strftime("%d-%m-%Y")
    return calendar


def _is_supervisor(ud: dict | None) -> bool:
    return bool(ud and ud.get("is_supervisor"))


def _is_rotational_shift(ud: dict | None) -> bool:
    return (ud or {}).get("shift_type", "").strip().upper() == "RS"


def _session_base(ud: dict) -> dict:
    return {
        "employee_name": ud.get("name") or "Employee",
        "department": ud.get("department") or "",
        "permission_date": _today_ddmmy(),
        "form_type": "PERMISSION_REQUEST",
    }


def try_start(sender: str, deps: PermissionDeps) -> None:
    exists, ud = get_user_record(sender)
    if not exists or not ud:
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return

    session = _session_base(ud)
    if _is_supervisor(ud):
        deps.session_merge(sender, state="WAITING_PERMISSION_FOR", **session)
        _send_for_buttons(sender, deps)
        return

    _start_myself_flow(sender, session, deps, ud)


def handle(sender: str, incoming: str, session: dict, deps: PermissionDeps) -> None:
    state = session.get("state")
    choice = _normalize_incoming(incoming)

    if state == "WAITING_PERMISSION_FOR":
        for_choice = _normalize_for_choice(incoming)
        if for_choice not in FOR_CHOICES:
            _send_for_buttons(sender, deps)
            return
        exists, ud = get_user_record(sender)
        if not exists or not ud:
            deps.send_to(sender, "User not registered.\nPlease contact admin.")
            return
        if for_choice == "PERMISSION_FOR_MYSELF":
            deps.session_merge(
                sender,
                permission_for="myself",
                **{k: session.get(k) for k in ("employee_name", "department", "permission_date", "form_type")},
            )
            _start_myself_flow(sender, session, deps, ud)
            return
        deps.session_merge(
            sender,
            state="WAITING_PERMISSION_CL_NAME",
            permission_for="cl",
        )
        deps.send_to(sender, "Please type the employee name:")
        return

    if state == "WAITING_PERMISSION_CL_NAME":
        if choice in FOR_CHOICES or choice in SHIFT_CHOICES or choice in TYPE_CHOICES:
            deps.send_to(sender, "Please type the employee name:")
            return
        cl_name = (incoming or "").strip()
        if not cl_name:
            deps.send_to(sender, "Please type the employee name:")
            return
        session = {**session, "cl_employee_name": cl_name[:200], "permission_for": "cl"}
        deps.session_merge(
            sender,
            state="WAITING_PERMISSION_SHIFT",
            cl_employee_name=cl_name[:200],
        )
        _send_shift_buttons(sender, deps)
        return

    if state == "WAITING_PERMISSION_SHIFT":
        shift_code = _normalize_shift_choice(incoming)
        if shift_code not in SHIFT_CHOICES:
            _send_shift_buttons(sender, deps)
            return
        shift_label = "I" if shift_code == "PERMISSION_SHIFT_I" else "II"
        permission_for = (session.get("permission_for") or "myself").strip().lower()
        if permission_for == "cl":
            exists, ud = get_user_record(sender)
            ud = ud or {}
            if _check_cl_overlap(
                sender,
                session,
                deps,
                (session.get("cl_employee_name") or "").strip(),
                work_date=_compute_permission_work_date(
                    ud,
                    permission_shift=shift_label,
                    permission_type_code="PERMISSION_EARLY_OUT",
                    permission_for="cl",
                ),
            ):
                return
            deps.session_merge(
                sender,
                state="WAITING_PERMISSION_REASON",
                permission_shift=shift_label,
            )
            deps.send_to(sender, "Please type your permission reason:")
            return
        deps.session_merge(
            sender,
            state="WAITING_PERMISSION_TYPE",
            permission_shift=shift_label,
        )
        _send_type_buttons(sender, deps)
        return

    if state == "WAITING_PERMISSION_TYPE":
        type_code = _normalize_type_choice(incoming)
        if type_code not in TYPE_CHOICES:
            _send_type_buttons(sender, deps)
            return
        deps.session_merge(
            sender,
            state="WAITING_PERMISSION_REASON",
            permission_type_code=type_code,
            permission_type=TYPE_LABELS[type_code],
        )
        deps.send_to(sender, "Please type your permission reason:")
        return

    if state == "WAITING_PERMISSION_REASON":
        if choice in FOR_CHOICES or choice in SHIFT_CHOICES or choice in TYPE_CHOICES:
            deps.send_to(sender, "Please type your permission reason:")
            return
        reason = (incoming or "").strip()
        if not reason:
            deps.send_to(sender, "Please type your permission reason:")
            return
        if choice in CANCEL_CHOICES:
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


def _start_myself_flow(
    sender: str, session: dict, deps: PermissionDeps, ud: dict
) -> None:
    session = {**session, "permission_for": "myself"}
    if _check_myself_overlap(sender, session, deps, ud):
        return
    base = _session_base(ud)
    if _is_rotational_shift(ud):
        deps.session_merge(
            sender,
            state="WAITING_PERMISSION_SHIFT",
            permission_for="myself",
            **base,
        )
        _send_shift_buttons(sender, deps)
        return
    deps.session_merge(
        sender,
        state="WAITING_PERMISSION_TYPE",
        permission_for="myself",
        **base,
    )
    _send_type_buttons(sender, deps)


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


def _check_myself_overlap(
    sender: str,
    session: dict,
    deps: PermissionDeps,
    ud: dict,
    *,
    work_date: str | None = None,
) -> bool:
    overlap_doc, overlap_status = find_overlapping_permission_request(
        deps.db,
        ud.get("employee_id") or "",
        work_date or _today_ddmmy(),
        employee_wa=sender,
    )
    if not overlap_status or not overlap_doc:
        return False
    _prompt_permission_cancel_or_exit(
        sender, session, deps, overlap_doc, overlap_status
    )
    return True


def _check_cl_overlap(
    sender: str,
    session: dict,
    deps: PermissionDeps,
    cl_name: str,
    *,
    work_date: str | None = None,
) -> bool:
    overlap_doc, overlap_status = find_overlapping_cl_permission_request(
        deps.db,
        cl_name,
        work_date or _today_ddmmy(),
    )
    if not overlap_status or not overlap_doc:
        return False
    _prompt_permission_cancel_or_exit(
        sender, session, deps, overlap_doc, overlap_status
    )
    return True


def _send_for_buttons(wa_id: str, deps: PermissionDeps) -> None:
    try:
        send_reply_buttons(
            wa_id_to_phone(wa_id),
            "Permission for:",
            [
                ("PERMISSION_FOR_MYSELF", "For Myself"),
                ("PERMISSION_FOR_CL", "For CL"),
            ],
            callback_data="permission-for",
        )
    except Exception:
        logger.exception("permission for buttons failed")
        deps.send_to(wa_id, "Permission for:\nChoose For Myself or For CL.")


def _send_shift_buttons(wa_id: str, deps: PermissionDeps) -> None:
    try:
        send_reply_buttons(
            wa_id_to_phone(wa_id),
            "Shift?",
            [
                ("PERMISSION_SHIFT_I", "Shift I"),
                ("PERMISSION_SHIFT_II", "Shift II"),
            ],
            callback_data="permission-shift",
        )
    except Exception:
        logger.exception("permission shift buttons failed")
        deps.send_to(wa_id, "Shift?\nChoose Shift I or Shift II.")


def _send_type_buttons(wa_id: str, deps: PermissionDeps) -> None:
    try:
        send_reply_buttons(
            wa_id_to_phone(wa_id),
            "Permission type:",
            [
                ("PERMISSION_LATE_IN", "Late IN"),
                ("PERMISSION_EARLY_OUT", "Early OUT"),
                ("PERMISSION_OTHER", "Other"),
            ],
            callback_data="permission-type",
        )
    except Exception:
        logger.exception("permission type buttons failed")
        deps.send_to(
            wa_id,
            "Permission type:\nChoose Late IN, Early OUT, or Other.",
        )


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

    permission_for = (session.get("permission_for") or "myself").strip().lower()
    shift = (session.get("permission_shift") or "").strip()
    if permission_for == "myself" and not _is_rotational_shift(ud):
        shift = "I"
    type_code = (
        "PERMISSION_EARLY_OUT"
        if permission_for == "cl"
        else (session.get("permission_type_code") or "")
    )
    work_date = _compute_permission_work_date(
        ud,
        permission_shift=shift,
        permission_type_code=type_code,
        permission_for=permission_for,
    )
    if permission_for == "cl":
        cl_name = (session.get("cl_employee_name") or "").strip()
        if not cl_name:
            deps.session_ref(sender).delete()
            deps.send_to(sender, "Employee name missing. Send Hi to start over.")
            return
        if _check_cl_overlap(sender, session, deps, cl_name, work_date=work_date):
            return
    elif _check_myself_overlap(sender, session, deps, ud, work_date=work_date):
        return

    employee_id = ud.get("employee_id") or ""
    permission_date = _today_ddmmy()
    chain = deps.build_approval_chain(ud, permission_for=permission_for)
    if not chain or not chain.get("jmd"):
        deps.session_ref(sender).delete()
        if permission_for == "cl":
            deps.send_to(
                sender,
                "CL permission approvers not configured.\n"
                "Set PPC_WHATSAPP_NUMBER and HR_WHATSAPP_NUMBER, or contact admin.",
            )
        else:
            deps.send_to(
                sender,
                "Permission approvers not configured.\nPlease contact admin.",
            )
        return
    if not chain.get("md"):
        deps.session_ref(sender).delete()
        deps.send_to(
            sender,
            "Permission approvers not configured.\nPlease contact admin.",
        )
        return

    perms_last_month, perms_current_month = get_employee_permission_counts(
        employee_id,
        employee_wa=sender,
        firestore_db=deps.db,
    )
    payload = {
        "request_id": "",
        "requested_datetime": deps.utcnow(),
        "employee": sender,
        "employee_id": employee_id,
        "employee_name": ud.get("name") or session.get("employee_name") or "Employee",
        "department": ud.get("department") or session.get("department") or "",
        "type": "PERMISSION",
        "permission_for": permission_for,
        "permission_shift": shift,
        "reason": reason,
        "permission_date": permission_date,
        "permission_work_date": work_date,
        "permissions_last_month": perms_last_month,
        "permissions_current_month": perms_current_month,
        "jmd": chain["jmd"],
        "jmd_route": chain["jmd_route"],
        "md": chain["md"],
        "permission_cl_chain": bool(chain.get("permission_cl_chain")),
        "manager_status": "N/A",
        "jmd_status": "PENDING",
        "md_status": "AWAITING_JMD",
        "source": "whatsapp_request",
    }

    if permission_for == "cl":
        payload["cl_employee_name"] = (session.get("cl_employee_name") or "").strip()
        payload["raised_by_name"] = payload["employee_name"]
        payload["permission_type"] = "Early OUT"
        payload["permission_type_code"] = "PERMISSION_EARLY_OUT"
    else:
        payload["permission_type"] = (
            session.get("permission_type")
            or TYPE_LABELS.get(session.get("permission_type_code") or "", "")
            or "Other"
        )
        payload["permission_type_code"] = session.get("permission_type_code") or ""

    ref = deps.db.collection("requests").document()
    request_id = ref.id
    payload["request_id"] = request_id
    ref.set(payload)
    logger.info(
        "PERMISSION created %s for=%s jmd_route=%s date=%s",
        request_id,
        permission_for,
        chain["jmd_route"],
        permission_date,
    )

    rd = ref.get().to_dict()
    jmd_ok = deps.notify_jmd(chain["jmd"], rd, request_id)

    deps.session_ref(sender).delete()
    msg = "Your permission request has been submitted for approval."
    if not jmd_ok:
        route = chain["jmd_route"]
        approver = "PPC" if permission_for == "cl" else f"JMD ({route})"
        msg += (
            f"\n\n{approver} could not be notified on WhatsApp. "
            "Ask them to send Hi to this Alubee number once, then contact admin."
        )
    deps.send_to(sender, msg)


def _normalize_incoming(incoming: str) -> str:
    return (incoming or "").strip().upper().replace(" ", "_")


def _normalize_for_choice(incoming: str) -> str:
    c = _normalize_incoming(incoming)
    if c in ("PERMISSION_FOR_MYSELF", "FOR_MYSELF", "MYSELF", "1"):
        return "PERMISSION_FOR_MYSELF"
    if c in ("PERMISSION_FOR_CL", "FOR_CL", "CL", "2"):
        return "PERMISSION_FOR_CL"
    low = (incoming or "").strip().lower()
    if low in ("for myself", "myself"):
        return "PERMISSION_FOR_MYSELF"
    if low in ("for cl", "cl"):
        return "PERMISSION_FOR_CL"
    return c


def _normalize_shift_choice(incoming: str) -> str:
    c = _normalize_incoming(incoming)
    if c in ("PERMISSION_SHIFT_I", "SHIFT_I", "SHI", "1"):
        return "PERMISSION_SHIFT_I"
    if c in ("PERMISSION_SHIFT_II", "SHIFT_II", "SHII", "2"):
        return "PERMISSION_SHIFT_II"
    low = (incoming or "").strip().lower()
    if low in ("shift i", "shift 1", "i"):
        return "PERMISSION_SHIFT_I"
    if low in ("shift ii", "shift 2", "ii"):
        return "PERMISSION_SHIFT_II"
    return c


def _normalize_type_choice(incoming: str) -> str:
    c = _normalize_incoming(incoming)
    if c in ("PERMISSION_LATE_IN", "LATE_IN", "LATEIN", "1"):
        return "PERMISSION_LATE_IN"
    if c in ("PERMISSION_EARLY_OUT", "EARLY_OUT", "EARLYOUT", "2"):
        return "PERMISSION_EARLY_OUT"
    if c in ("PERMISSION_OTHER", "OTHER", "3"):
        return "PERMISSION_OTHER"
    low = (incoming or "").strip().lower()
    if low in ("late in", "late"):
        return "PERMISSION_LATE_IN"
    if low in ("early out", "early"):
        return "PERMISSION_EARLY_OUT"
    if low == "other":
        return "PERMISSION_OTHER"
    return c


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
