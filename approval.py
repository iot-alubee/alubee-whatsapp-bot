"""
Shared JMD → MD approval (OD, visitor, and future request types).
Configured from main.py after Firestore and env are ready.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from bot_shared import (
    format_leave_days_label,
    get_employee_leave_counts,
    get_employee_permission_counts,
    get_user_record,
    leave_days_requested_from_doc,
    leave_days_value_from_doc,
    shrink_leave_to_day_count,
    wa_from_env,
)

import approver_availability

from interakt_api import ensure_customer, send_reply_buttons, wa_id_to_phone

logger = logging.getLogger(__name__)

WAITING_LEAVE_DAYS_MODIFY = "WAITING_LEAVE_DAYS_MODIFY"

VISITING_UNIT_I = "UNIT_I"
VISITING_UNIT_II = "UNIT_II"
VISITING_BOTH = "BOTH"

VISITING_TO_LABELS = {
    VISITING_UNIT_I: "Unit I",
    VISITING_UNIT_II: "Unit II",
    VISITING_BOTH: "Both",
}

_VISITING_TO_ALIASES = {
    "unit_i": VISITING_UNIT_I,
    "unit_ii": VISITING_UNIT_II,
    "unit_1": VISITING_UNIT_I,
    "unit_2": VISITING_UNIT_II,
    "both": VISITING_BOTH,
    "visitor_visit_unit_i": VISITING_UNIT_I,
    "visitor_visit_unit_ii": VISITING_UNIT_II,
    "visitor_visit_both": VISITING_BOTH,
}


def normalize_visiting_to(visiting_to: str) -> str:
    """Map button ids / labels to UNIT_I, UNIT_II, or BOTH."""
    raw = (visiting_to or "").strip()
    if not raw:
        return VISITING_UNIT_I
    upper = raw.upper()
    if upper in (VISITING_UNIT_I, VISITING_UNIT_II, VISITING_BOTH):
        return upper
    slug = raw.lower().replace(" ", "_").replace("-", "_")
    return _VISITING_TO_ALIASES.get(slug, VISITING_UNIT_I)


@dataclass
class ApprovalDeps:
    db: object
    send_to: Callable[[str, str], None]
    session_merge: Callable[..., None]
    session_ref: Callable[[str], object]
    utcnow: Callable
    chat_name: Callable[[str], str]
    same_whatsapp: Callable[[str, str], bool]
    has_active_whatsapp_session: Callable[[str], bool]
    jmd_i: str
    jmd_ii: str
    md: str
    whatsapp_session_hours: int
    menu_idle_state: str
    on_visitor_md_approved: Callable[[object, dict], None]
    test_md: str = ""
    # Visitor uses same approvers as OD (jmd_i / jmd_ii / md); fields kept for optional overrides.
    visitor_jmd_i: str = ""
    visitor_jmd_ii: str = ""
    visitor_md: str = ""
    # If true, Unit II employees (jmd_route JMD2) use jmd_ii; else everyone uses jmd_i.
    visitor_route_by_unit: bool = False
    # Optional: listed employees use test numbers instead (for pilot testing).
    visitor_test_jmd_i: str = ""
    visitor_test_jmd_ii: str = ""
    visitor_test_md: str = ""
    visitor_test_employee_wa_ids: frozenset[str] = frozenset()
    ppc: str = ""
    hr: str = ""


_deps: ApprovalDeps | None = None


def configure(deps: ApprovalDeps) -> None:
    global _deps
    _deps = deps


def _require() -> ApprovalDeps:
    if _deps is None:
        raise RuntimeError("approval.configure() not called from main.py")
    return _deps


def _use_visitor_test_approvers(employee_wa: str) -> bool:
    d = _require()
    if not d.visitor_test_employee_wa_ids or not employee_wa:
        return False
    return employee_wa.strip().lower() in d.visitor_test_employee_wa_ids


def _visitor_approver_numbers(
    d: ApprovalDeps, *, use_test: bool
) -> tuple[str, str, str]:
    """Same env as OD: JMD_I / JMD_II / MD (prefer live Cloud Run vars at request time)."""
    live_i = wa_from_env("JMD_I_WHATSAPP_NUMBER", "JMD_WHATSAPP_NUMBER")
    live_ii = wa_from_env("JMD_II_WHATSAPP_NUMBER")
    live_md = wa_from_env("MD_WHATSAPP_NUMBER")
    jmd_i_od = live_i or d.visitor_jmd_i or d.jmd_i
    jmd_ii_od = live_ii or d.visitor_jmd_ii or d.jmd_ii
    md_od = live_md or d.visitor_md or d.md
    if use_test:
        jmd_i = (
            wa_from_env(
                "VISITOR_TEST_JMD_I_WHATSAPP_NUMBER",
                "VISITOR_TEST_JMD_WHATSAPP_NUMBER",
            )
            or d.visitor_test_jmd_i
            or jmd_i_od
        )
        jmd_ii = (
            wa_from_env("VISITOR_TEST_JMD_II_WHATSAPP_NUMBER")
            or d.visitor_test_jmd_ii
            or jmd_ii_od
        )
        md = (
            wa_from_env("VISITOR_TEST_MD_WHATSAPP_NUMBER")
            or d.visitor_test_md
            or md_od
        )
    else:
        jmd_i = jmd_i_od
        jmd_ii = jmd_ii_od
        md = md_od
    return jmd_i, jmd_ii, md


def visitor_chain_failure_message(
    user_data: dict | None,
    *,
    visiting_to: str = "",
    employee_wa: str = "",
) -> str:
    """User-facing hint when build_approval_chain returns None."""
    d = _require()
    use_test = _use_visitor_test_approvers(employee_wa)
    jmd_i, jmd_ii, md = _visitor_approver_numbers(d, use_test=use_test)
    vt = normalize_visiting_to(visiting_to)
    if vt == VISITING_BOTH:
        if not md:
            return (
                "MD is not configured on the server "
                "(MD_WHATSAPP_NUMBER).\nPlease contact admin."
            )
        if not jmd_i:
            return (
                "Unit I JMD is not configured "
                "(JMD_I_WHATSAPP_NUMBER).\nPlease contact admin."
            )
        if not jmd_ii:
            return (
                "Unit II JMD is not configured "
                "(JMD_II_WHATSAPP_NUMBER).\n"
                "Add it in Cloud Run → Variables, then deploy a new revision.\n"
                "Please contact admin."
            )
        if d.same_whatsapp(jmd_i, jmd_ii):
            return (
                "Unit I and Unit II visitor JMD must use different WhatsApp numbers.\n"
                "Please contact admin."
            )
    return (
        "Visitor approvers are not configured on the server.\nPlease contact admin."
    )


def _build_visitor_approval_chain(
    d: ApprovalDeps,
    user_data: dict,
    employee_wa: str,
    visiting_to: str,
) -> dict | None:
    """Route visitor JMD(s) by destination unit vs employee home unit (jmd_route)."""
    use_test = _use_visitor_test_approvers(employee_wa)
    jmd_i, jmd_ii, md = _visitor_approver_numbers(d, use_test=use_test)
    emp_route = (user_data.get("jmd_route") or "JMD1").strip().upper()
    vt = normalize_visiting_to(visiting_to)

    if not md:
        logger.error("visitor MD not configured — set MD_WHATSAPP_NUMBER")
        return None

    if vt == VISITING_BOTH:
        if not jmd_i or not jmd_ii:
            logger.error(
                "visitor BOTH requires JMD_I and JMD_II on Cloud Run "
                "(jmd_i=%r jmd_ii=%r md=%r)",
                jmd_i or "(missing)",
                jmd_ii or "(missing)",
                md or "(missing)",
            )
            return None
        if d.same_whatsapp(jmd_i, jmd_ii):
            logger.error(
                "visitor BOTH: JMD_II_WHATSAPP_NUMBER must differ from "
                "JMD_I_WHATSAPP_NUMBER"
            )
            return None
        chain: dict = {
            "mode": "dual",
            "visiting_to": vt,
            "employee_jmd_route": emp_route,
            "jmd_i": jmd_i,
            "jmd_ii": jmd_ii,
            "md": md,
            "visitor_approval": True,
        }
    else:
        cross = False
        if vt == VISITING_UNIT_I:
            host_jmd = jmd_i
            host_route = "JMD1"
            cross = emp_route == "JMD2"
        elif vt == VISITING_UNIT_II:
            host_jmd = jmd_ii or jmd_i
            host_route = "JMD2"
            cross = emp_route == "JMD1"
        else:
            host_jmd = _visitor_jmd_for_route(d, emp_route, use_test=use_test)
            host_route = emp_route
        if not host_jmd:
            logger.error(
                "visitor approvers not configured — set JMD_I_WHATSAPP_NUMBER "
                "(and JMD_II_WHATSAPP_NUMBER for Unit II / Both)"
            )
            return None
        chain = {
            "mode": "single",
            "visiting_to": vt,
            "employee_jmd_route": emp_route,
            "jmd": host_jmd,
            "jmd_route": host_route,
            "md": md,
            "cross": cross,
            "visitor_approval": True,
        }
    if use_test:
        chain["approval_test"] = True
    return chain


def _visitor_jmd_for_route(d: ApprovalDeps, jmd_route: str, *, use_test: bool) -> str:
    """Same JMD numbers as OD (jmd_i / jmd_ii). Default: Unit I JMD for every employee."""
    route = (jmd_route or "").strip().upper()
    by_unit = d.visitor_route_by_unit and route == "JMD2"
    jmd_i = d.visitor_jmd_i or d.jmd_i
    jmd_ii = d.visitor_jmd_ii or d.jmd_ii
    if use_test and d.visitor_test_jmd_i:
        if by_unit and d.visitor_test_jmd_ii:
            return d.visitor_test_jmd_ii
        return d.visitor_test_jmd_i
    if jmd_i:
        if by_unit and jmd_ii:
            return jmd_ii
        return jmd_i
    return ""


def jmd_whatsapp_for_route(jmd_route: str, *, for_request_type: str = "OD") -> str:
    """OD approvers only (visitor uses build_approval_chain)."""
    d = _require()
    if (for_request_type or "").strip().upper() == "VISITOR":
        jmd = _visitor_jmd_for_route(d, jmd_route, use_test=False)
        if jmd:
            return jmd
    if (jmd_route or "").strip().upper() == "JMD2":
        return d.jmd_ii
    return d.jmd_i


def request_md_whatsapp(rd: dict) -> str:
    stored = (rd.get("md") or "").strip()
    if stored and (rd.get("type") or "").strip().upper() == "VISITOR":
        return stored
    if stored:
        return stored
    d = _require()
    if (rd.get("type") or "").strip().upper() == "VISITOR":
        return d.visitor_md or d.md
    return d.md


def request_jmd_whatsapp(rd: dict) -> str:
    stored = (rd.get("jmd") or "").strip()
    if stored and (rd.get("type") or "").strip().upper() == "VISITOR":
        return stored
    if rd.get("approval_test") and stored:
        return stored
    route = (rd.get("jmd_route") or "").strip().upper()
    if route in ("JMD1", "JMD2"):
        req_type = (rd.get("type") or "OD").strip().upper()
        return jmd_whatsapp_for_route(route, for_request_type=req_type)
    if stored:
        return stored
    return _require().jmd_i


def build_approval_chain(
    user_data: dict | None = None,
    *,
    request_type: str = "OD",
    employee_wa: str = "",
    visiting_to: str = "",
) -> dict | None:
    if not user_data:
        return None
    d = _require()
    jmd_route = (user_data.get("jmd_route") or "JMD1").strip().upper()
    req_type = (request_type or "OD").strip().upper()

    if req_type == "VISITOR":
        return _build_visitor_approval_chain(
            d, user_data, employee_wa, normalize_visiting_to(visiting_to)
        )

    jmd = jmd_whatsapp_for_route(jmd_route, for_request_type="OD")
    md = d.md
    if not jmd or not md:
        return None
    return {"jmd": jmd, "jmd_route": jmd_route, "md": md}


def build_leave_approval_chain(user_data: dict | None = None) -> dict | None:
    """Leave: same JMD → MD chain as OD."""
    return build_approval_chain(user_data, request_type="LEAVE")


def build_permission_approval_chain(
    user_data: dict | None = None,
    *,
    permission_for: str = "myself",
) -> dict | None:
    """
    Employee permission (myself): JMD → MD (same as OD).
    CL permission: PPC → HR (no online/offline for PPC/HR).
    """
    if not user_data:
        return None
    pf = (permission_for or "myself").strip().lower()
    if pf == "cl":
        d = _require()
        ppc = (wa_from_env("PPC_WHATSAPP_NUMBER") or d.ppc or "").strip()
        hr = (wa_from_env("HR_WHATSAPP_NUMBER") or d.hr or "").strip()
        if not ppc or not hr:
            logger.error(
                "CL permission approvers missing ppc=%r hr=%r",
                ppc or "(missing)",
                hr or "(missing)",
            )
            return None
        return {
            "jmd": ppc,
            "jmd_route": "PPC",
            "md": hr,
            "permission_cl_chain": True,
        }
    return build_approval_chain(user_data, request_type="PERMISSION")


def _approval_message_body(
    *,
    employee_name: str,
    department: str,
    reason: str,
    request_rd: dict | None = None,
) -> str:
    d = _require()
    req_type = ((request_rd or {}).get("type") or "OD").strip().upper()
    emp = d.chat_name(employee_name)
    dept = department or "—"
    if req_type == "VISITOR":
        rd = request_rd or {}
        raw_names = rd.get("visitor_names") or []
        if isinstance(raw_names, str):
            names = raw_names.strip() or "—"
        else:
            names = ", ".join(raw_names) or "—"
        coming_on = (rd.get("coming_on_date") or rd.get("visit_date") or "").strip() or "—"
        coming_from = (
            (rd.get("coming_from") or rd.get("coming_from_label") or rd.get("organization") or "")
            .strip()
            or "—"
        )
        coming_for = (
            (
                rd.get("purpose_label")
                or rd.get("coming_for_label")
                or rd.get("visit_for_label")
                or ""
            ).strip()
            or "—"
        )
        visiting = (
            (rd.get("visiting_to_label") or "").strip()
            or VISITING_TO_LABELS.get(
                (rd.get("visiting_to") or "").strip().upper(), ""
            )
            or "—"
        )
        test_tag = "[TEST] " if rd.get("approval_test") else ""
        return (
            f"{test_tag}Visitor approval request\n\n"
            f"Employee: {emp}\n"
            f"Department: {dept}\n"
            f"Visiting to: {visiting}\n"
            f"Coming on: {coming_on}\n"
            f"People: {rd.get('people_count') or '—'}\n"
            f"Names: {names}\n"
            f"Coming from: {coming_from}\n"
            f"Purpose: {coming_for}\n"
            f"Guest WhatsApp: {rd.get('guest_phone') or '—'}\n\n"
            "Please approve or deny."
        )
    if req_type == "LEAVE":
        rd = request_rd or {}
        from_d = (rd.get("leave_from_date") or "").strip()
        to_d = (rd.get("leave_to_date") or from_d).strip()
        days_label = format_leave_days_label(rd)
        days_val = leave_days_value_from_doc(rd)
        if days_val <= 1 or (from_d and to_d and from_d == to_d):
            date_lines = f"Date: {from_d or '—'}\n"
        else:
            date_lines = f"From Date: {from_d or '—'}\nTo Date: {to_d or '—'}\n"
        leaves_last = 0
        leaves_curr = 0
        eid = (rd.get("employee_id") or "").strip()
        if eid:
            try:
                leaves_last, leaves_curr = get_employee_leave_counts(
                    eid,
                    employee_wa=(rd.get("employee") or "").strip(),
                    firestore_db=_require().db,
                )
            except Exception:
                logger.exception("leave count lookup failed employee_id=%s", eid)
                leaves_last = rd.get("leaves_last_month", 0)
                leaves_curr = rd.get("leaves_current_month", 0)
        test_tag = "[TEST] " if rd.get("leave_test_approver") else ""
        return (
            f"{test_tag}Leave approval request\n\n"
            f"Name: {emp}\n"
            f"Department: {dept}\n"
            f"No. of days leave: {days_label}\n"
            f"{date_lines}"
            f"Reason: {reason or '—'}\n"
            f"Leaves in Last month: {leaves_last}\n"
            f"Leaves in current month: {leaves_curr}\n\n"
            "Please approve, deny, or modify days."
        )
    if req_type == "PERMISSION":
        rd = request_rd or {}

        def _permission_shift_display() -> str:
            raw = (rd.get("permission_shift") or "").strip().upper()
            if raw in ("I", "1"):
                return "I"
            if raw in ("II", "2"):
                return "II"
            wa = (rd.get("employee") or "").strip()
            if wa:
                exists, ud = get_user_record(wa)
                if exists and ud:
                    if (ud.get("shift_type") or "GS").strip().upper() == "GS":
                        return "I"
            return raw or "—"

        perms_last = 0
        perms_curr = 0
        eid = (rd.get("employee_id") or "").strip()
        if eid:
            try:
                perms_last, perms_curr = get_employee_permission_counts(
                    eid,
                    employee_wa=(rd.get("employee") or "").strip(),
                    firestore_db=_require().db,
                )
            except Exception:
                logger.exception("permission count lookup failed employee_id=%s", eid)
                perms_last = rd.get("permissions_last_month", 0)
                perms_curr = rd.get("permissions_current_month", 0)
        perm_type = (rd.get("permission_type") or "").strip() or "—"
        test_tag = "[TEST] " if rd.get("permission_test_approver") else ""
        if (rd.get("permission_for") or "").strip().lower() == "cl":
            cl_name = (rd.get("cl_employee_name") or "").strip() or "—"
            shift = _permission_shift_display()
            return (
                f"{test_tag}Permission approval request (CL)\n\n"
                f"CL name: {cl_name}\n"
                f"Department: {dept}\n"
                f"Shift: {shift}\n"
                f"Reason: {reason or '—'}\n"
                f"Permission type: Early OUT\n\n"
                "Please approve or deny."
            )
        shift = _permission_shift_display()
        return (
            f"{test_tag}Permission approval request\n\n"
            f"Name: {emp}\n"
            f"Department: {dept}\n"
            f"Shift: {shift}\n"
            f"Permission type: {perm_type}\n"
            f"Reason: {reason or '—'}\n"
            f"Permission in last month: {perms_last}\n"
            f"Permission in current month: {perms_curr}\n\n"
            "Please approve or deny."
        )
    return (
        "OD approval request\n\n"
        f"Employee: {emp}\n"
        f"Department: {dept}\n"
        f"Reason: {reason or '—'}\n\n"
        "Please approve or deny."
    )


def _leave_modify_days_allowed(rd: dict | None) -> bool:
    if (rd or {}).get("type", "").strip().upper() != "LEAVE":
        return False
    return leave_days_requested_from_doc(rd or {}) > 1


def _leave_approval_button_rows(request_id: str, request_rd: dict | None) -> list[tuple[str, str]]:
    rid = (request_id or "").strip()
    rows = [
        (f"APPROVE_{rid}"[:256], "Approve"),
        (f"DENY_{rid}"[:256], "Deny"),
    ]
    if _leave_modify_days_allowed(request_rd):
        rows.append((f"MODIFY_DAYS_{rid}"[:256], "Modify Days"))
    return rows


def send_approval_buttons(
    wa_id: str,
    *,
    employee_name: str,
    department: str,
    reason: str,
    request_id: str,
    request_rd: dict | None = None,
) -> bool:
    d = _require()
    if not d.has_active_whatsapp_session(wa_id):
        logger.info(
            "skip approval notify to=%s request_id=%s (no active WhatsApp session in %sh)",
            wa_id,
            request_id,
            d.whatsapp_session_hours,
        )
        return False

    rid = (request_id or "").strip()
    body = _approval_message_body(
        employee_name=employee_name,
        department=department,
        reason=reason,
        request_rd=request_rd,
    )
    buttons = _leave_approval_button_rows(rid, request_rd)
    try:
        send_reply_buttons(
            wa_id_to_phone(wa_id),
            body,
            buttons,
            callback_data=request_id,
            ensure_contact=True,
            contact_name=d.chat_name(employee_name),
        )
        return True
    except Exception as e:
        logger.exception("approval buttons failed to=%s: %s", wa_id, e)
        try:
            ensure_customer(wa_id_to_phone(wa_id), name="Approver")
            send_reply_buttons(
                wa_id_to_phone(wa_id),
                body,
                buttons,
                callback_data=request_id,
            )
            return True
        except Exception:
            logger.exception("approval retry failed to=%s", wa_id)
        return False


def _leave_approver_can_modify(role: str | None, rd: dict) -> bool:
    if role not in ("jmd", "md"):
        return False
    if (rd.get("type") or "").strip().upper() != "LEAVE":
        return False
    jmd = (rd.get("jmd_status") or "").strip().upper()
    md = (rd.get("md_status") or "").strip().upper()
    if role == "jmd":
        return jmd in ("PENDING", "AWAITING_MANAGER", "AWAITING_JMD")
    return jmd == "APPROVED" and md == "PENDING"


def _leave_days_modify_prompt(rd: dict) -> str:
    max_days = leave_days_requested_from_doc(rd)
    current_label = format_leave_days_label(rd)
    max_label = format_leave_days_label({
        **rd,
        "leave_days": max_days,
        "leave_duration": "full" if max_days > 0.5 else "half",
    })
    return (
        f"Employee requested: {max_label}\n"
        f"JMD set to: {current_label}\n"
        f"Reply with a number from 1 to {int(max_days)}."
    )


def _send_leave_day_picker(sender: str, rd: dict, request_id: str) -> None:
    d = _require()
    d.send_to(sender, _leave_days_modify_prompt(rd))


def _resend_leave_approval_prompt(sender: str, rd: dict, request_id: str) -> None:
    d = _require()
    d.session_merge(
        sender,
        state="WAITING_APPROVAL_ACTION",
        approval_request_id=request_id,
    )
    send_approval_buttons(
        sender,
        employee_name=rd.get("employee_name") or "Employee",
        department=rd.get("department") or "",
        reason=rd.get("reason") or "",
        request_id=request_id,
        request_rd=rd,
    )


def _apply_leave_day_count(sender: str, request_id: str, day_count: int) -> bool:
    d = _require()
    ref = d.db.collection("requests").document(request_id)
    snap = ref.get()
    if not snap.exists:
        d.send_to(sender, "This leave request is no longer available.")
        return True
    rd = snap.to_dict() or {}
    role = _approval_role(sender, rd)
    if not _leave_approver_can_modify(role, rd):
        d.send_to(sender, "You cannot modify days for this leave request.")
        return True
    patch = shrink_leave_to_day_count(rd, day_count)
    if not patch:
        max_days = leave_days_requested_from_doc(rd)
        d.send_to(
            sender,
            f"Invalid number of days. Enter a whole number from 1 to {max_days}.",
        )
        _resend_leave_approval_prompt(sender, rd, request_id)
        return True
    ref.update(patch)
    rd = ref.get().to_dict() or rd
    d.send_to(
        sender,
        f"Leave days updated to {patch['leave_days']}.\nYou can approve, deny, or modify again.",
    )
    _resend_leave_approval_prompt(sender, rd, request_id)
    logger.info(
        "leave days modified request_id=%s by=%s role=%s days=%s",
        request_id,
        sender,
        role,
        patch["leave_days"],
    )
    return True


def handle_leave_modify_gate(sender: str, incoming: str) -> bool:
    """Modify-days flow for leave (JMD / MD). Returns True if handled."""
    d = _require()
    raw = (incoming or "").strip()
    upper = raw.upper()

    if upper.startswith("MODIFY_DAYS_"):
        request_id = raw[len("MODIFY_DAYS_") :].strip()
        if not request_id:
            return False
        ref = d.db.collection("requests").document(request_id)
        snap = ref.get()
        if not snap.exists:
            d.send_to(sender, "This leave request is no longer available.")
            return True
        rd = snap.to_dict() or {}
        role = _approval_role(sender, rd)
        if not _leave_approver_can_modify(role, rd):
            d.send_to(sender, "You cannot modify days for this leave request.")
            return True
        if not _leave_modify_days_allowed(rd):
            d.send_to(sender, "This leave is only 1 day — modification is not available.")
            _resend_leave_approval_prompt(sender, rd, request_id)
            return True
        d.session_merge(
            sender,
            state=WAITING_LEAVE_DAYS_MODIFY,
            approval_request_id=request_id,
        )
        _send_leave_day_picker(sender, rd, request_id)
        return True

    if upper.startswith("LEAVE_DAYS_"):
        tail = upper[len("LEAVE_DAYS_") :]
        if not tail.isdigit():
            return False
        day_count = int(tail)
        snap = d.session_ref(sender).get()
        if not snap.exists:
            return False
        data = snap.to_dict() or {}
        if data.get("state") != WAITING_LEAVE_DAYS_MODIFY:
            return False
        request_id = (data.get("approval_request_id") or "").strip()
        if not request_id:
            return False
        return _apply_leave_day_count(sender, request_id, day_count)

    snap = d.session_ref(sender).get()
    if not snap.exists:
        return False
    data = snap.to_dict() or {}
    if data.get("state") != WAITING_LEAVE_DAYS_MODIFY:
        return False
    request_id = (data.get("approval_request_id") or "").strip()
    if not request_id:
        return False
    if upper in ("APPROVE", "DENY") or upper.startswith("APPROVE_") or upper.startswith("DENY_"):
        d.session_merge(sender, state="WAITING_APPROVAL_ACTION", approval_request_id=request_id)
        return False
    if upper.startswith("MODIFY_DAYS_"):
        return False
    try:
        day_count = int(raw)
    except ValueError:
        rd = d.db.collection("requests").document(request_id).get().to_dict() or {}
        max_days = leave_days_requested_from_doc(rd)
        d.send_to(
            sender,
            f"Invalid number of days. Enter a whole number from 1 to {max_days}.",
        )
        _resend_leave_approval_prompt(sender, rd, request_id)
        return True
    return _apply_leave_day_count(sender, request_id, day_count)


def _set_pending_approval(recipient: str, request_id: str) -> None:
    d = _require()
    d.session_merge(
        recipient,
        state="WAITING_APPROVAL_ACTION",
        approval_request_id=request_id,
    )


def resolve_approval(incoming: str, approver: str):
    d = _require()
    raw = (incoming or "").strip()
    upper = raw.upper()
    if upper.startswith("APPROVE_"):
        rid = raw.split("_", 1)[1].strip()
        return (True, rid) if rid else (None, None)
    if upper.startswith("DENY_"):
        rid = raw.split("_", 1)[1].strip()
        return (False, rid) if rid else (None, None)
    if upper in ("APPROVE", "DENY"):
        snap = d.session_ref(approver).get()
        if snap.exists:
            data = snap.to_dict() or {}
            if data.get("state") == "WAITING_APPROVAL_ACTION":
                rid = (data.get("approval_request_id") or "").strip()
                if rid:
                    return upper == "APPROVE", rid
    return None, None


def _approval_role(sender: str, rd: dict) -> str | None:
    d = _require()
    jmd_st = (rd.get("jmd_status") or "").strip().upper()
    md_st = (rd.get("md_status") or "").strip().upper()
    req_type = (rd.get("type") or "").strip().upper()

    if req_type == "LEAVE" and rd.get("leave_test_approver"):
        test_md = (wa_from_env("TEST_MD_WHATSAPP_NUMBER") or d.test_md or "").strip()
        if (
            test_md
            and d.same_whatsapp(sender, test_md)
            and jmd_st in ("PENDING", "AWAITING_MANAGER")
        ):
            return "jmd"

    if req_type == "PERMISSION" and rd.get("permission_test_approver"):
        test_md = (wa_from_env("TEST_MD_WHATSAPP_NUMBER") or d.test_md or "").strip()
        if (
            test_md
            and d.same_whatsapp(sender, test_md)
            and jmd_st in ("PENDING", "AWAITING_MANAGER")
        ):
            return "jmd"

    if approver_availability.is_test_md_sender(sender, d.test_md, d.same_whatsapp):
        return None

    if rd.get("visitor_dual_jmd"):
        jmd_i_st = (rd.get("jmd_i_status") or "").strip().upper()
        jmd_ii_st = (rd.get("jmd_ii_status") or "").strip().upper()
        if d.same_whatsapp(sender, (rd.get("jmd_i") or "")) and jmd_i_st == "PENDING":
            return "jmd_i"
        if d.same_whatsapp(sender, (rd.get("jmd_ii") or "")) and jmd_ii_st == "PENDING":
            return "jmd_ii"
        md_wa = request_md_whatsapp(rd)
        if _md_offline_closed(rd):
            return None
        if (
            d.same_whatsapp(sender, md_wa)
            and jmd_i_st == "APPROVED"
            and jmd_ii_st == "APPROVED"
            and md_st == "PENDING"
        ):
            return "md"
        return None

    jmd_wa = request_jmd_whatsapp(rd)
    if d.same_whatsapp(sender, jmd_wa) and jmd_st in ("PENDING", "AWAITING_MANAGER"):
        return "jmd"

    md_wa = request_md_whatsapp(rd)
    if _md_offline_closed(rd):
        return None
    if d.same_whatsapp(sender, md_wa) and jmd_st == "APPROVED" and md_st == "PENDING":
        return "md"

    return None


def _request_cancelled_by_employee(rd: dict) -> bool:
    req_type = (rd.get("type") or "").strip().upper()
    if req_type not in ("LEAVE", "PERMISSION"):
        return False
    jmd = (rd.get("jmd_status") or "").strip().upper()
    if jmd == "CANCELLED":
        return True
    return bool(rd.get("cancelled_by_employee")) and jmd == "DENIED"


def _is_single_jmd_approver_sender(sender: str, rd: dict) -> bool:
    """True if sender is the JMD (or test MD) notified for leave/permission."""
    d = _require()
    if rd.get("leave_test_approver") or rd.get("permission_test_approver"):
        test_md = (wa_from_env("TEST_MD_WHATSAPP_NUMBER") or d.test_md or "").strip()
        if test_md and d.same_whatsapp(sender, test_md):
            return True
    return d.same_whatsapp(sender, request_jmd_whatsapp(rd))


def _dual_jmd_both_approved(rd: dict) -> bool:
    return (
        (rd.get("jmd_i_status") or "").strip().upper() == "APPROVED"
        and (rd.get("jmd_ii_status") or "").strip().upper() == "APPROVED"
    )


def _visitor_jmd_fully_approved(rd: dict) -> bool:
    if rd.get("visitor_dual_jmd"):
        return _dual_jmd_both_approved(rd)
    return (rd.get("jmd_status") or "").strip().upper() == "APPROVED"


def _md_is_offline_now(d: ApprovalDeps, md_wa: str) -> bool:
    return bool(md_wa and approver_availability.is_offline(d.db, md_wa))


def _md_offline_closed(rd: dict) -> bool:
    """MD step finished via offline bypass — must not ask MD again when back online."""
    if rd.get("md_offline_bypass"):
        return True
    return (rd.get("md_status") or "").strip().upper() == "OFFLINE"


def _md_status_after_jmd(md_offline: bool) -> str:
    return "OFFLINE" if md_offline else "PENDING"


def _md_offline_bypass_fields(d: ApprovalDeps, md_offline: bool) -> dict:
    if not md_offline:
        return {}
    return {
        "md_offline_bypass": True,
        "approved_datetime": d.utcnow(),
    }


def _after_jmd_when_md_offline(
    d: ApprovalDeps,
    ref,
    rd: dict,
    md_wa: str,
    *,
    employee: str,
    req_label: str,
    request_id: str,
) -> None:
    """Close MD on request + employee/visitor completion; do not leave MD pending."""
    fresh_snap = ref.get()
    rd_fresh = fresh_snap.to_dict() if fresh_snap.exists else rd
    if req_label == "visitor":
        if _visitor_jmd_fully_approved(rd_fresh):
            d.on_visitor_md_approved(ref, rd_fresh)
    else:
        d.send_to(employee, _employee_final_approval_message(req_label, rd_fresh))
    logger.info(
        "md offline bypass request_id=%s type=%s (md_status=OFFLINE)",
        request_id,
        req_label,
    )


def notify_visitor_on_submit(rd: dict, request_id: str, chain: dict) -> bool:
    """Notify host-unit JMD(s) when a visitor request is submitted."""
    d = _require()
    if chain.get("mode") == "dual":
        jmd_i = (chain.get("jmd_i") or "").strip()
        jmd_ii = (chain.get("jmd_ii") or "").strip()
        ok_i = notify_jmd(jmd_i, rd, request_id) if jmd_i else False
        ok_ii = False
        if jmd_ii and not d.same_whatsapp(jmd_i, jmd_ii):
            ok_ii = notify_jmd(jmd_ii, rd, request_id)
        elif jmd_ii:
            logger.warning(
                "visitor dual notify: JMD II same as JMD I request_id=%s", request_id
            )
        if not ok_i:
            logger.error(
                "visitor JMD I notify failed request_id=%s jmd_i=%s",
                request_id,
                jmd_i,
            )
        if not ok_ii:
            logger.error(
                "visitor JMD II notify failed request_id=%s jmd_ii=%s",
                request_id,
                jmd_ii,
            )
        logger.info(
            "visitor dual notify request_id=%s jmd_i_ok=%s jmd_ii_ok=%s",
            request_id,
            ok_i,
            ok_ii,
        )
        return ok_i and ok_ii
    jmd = chain.get("jmd") or ""
    return notify_jmd(jmd, rd, request_id) if jmd else False


def notify_jmd(jmd_wa: str, rd: dict, request_id: str) -> bool:
    if not jmd_wa:
        return False
    ok = send_approval_buttons(
        jmd_wa,
        employee_name=rd.get("employee_name"),
        department=rd.get("department"),
        reason=rd.get("reason"),
        request_id=request_id,
        request_rd=rd,
    )
    if ok:
        _set_pending_approval(jmd_wa, request_id)
    return ok


def notify_approver(wa_id: str, rd: dict, request_id: str) -> None:
    if not wa_id:
        return
    if send_approval_buttons(
        wa_id,
        employee_name=rd.get("employee_name"),
        department=rd.get("department"),
        reason=rd.get("reason"),
        request_id=request_id,
        request_rd=rd,
    ):
        _set_pending_approval(wa_id, request_id)


def notify_pending_leave_md_approvals(sender: str, *, limit: int = 10) -> int:
    """When MD goes online, prompt for leave rows awaiting MD (jmd approved, md pending)."""
    d = _require()
    if not sender:
        return 0
    notified = 0
    try:
        for snap in d.db.collection("requests").where("type", "==", "LEAVE").stream():
            rd = snap.to_dict() or {}
            if (rd.get("jmd_status") or "").strip().upper() != "APPROVED":
                continue
            if (rd.get("md_status") or "").strip().upper() != "PENDING":
                continue
            md_wa = request_md_whatsapp(rd)
            if not d.same_whatsapp(sender, md_wa):
                continue
            rid = (rd.get("request_id") or snap.id or "").strip()
            if not rid:
                continue
            notify_approver(md_wa, rd, rid)
            notified += 1
            if notified >= limit:
                break
    except Exception:
        logger.exception("pending leave MD notify failed sender=%s", sender)
        return notified
    if notified:
        logger.info("notified MD of %s pending leave(s) sender=%s", notified, sender)
    return notified


def _employee_final_approval_message(req_label: str, rd: dict | None = None) -> str:
    if req_label == "leave":
        data = rd or {}
        days_label = format_leave_days_label(data)
        days_val = leave_days_value_from_doc(data)
        from_d = (data.get("leave_from_date") or "").strip()
        to_d = (data.get("leave_to_date") or from_d).strip()
        msg = f"Your leave request has been approved for {days_label}."
        if days_val <= 1 or (from_d and to_d and from_d == to_d):
            if from_d:
                msg += f"\nDate: {from_d}"
        elif from_d:
            msg += f"\nFrom: {from_d}\nTo: {to_d}"
        return msg
    if req_label == "permission":
        return "Your permission request has been approved."
    if req_label == "visitor":
        return "Your visitor request has been approved."
    return "Your OD has been Approved."


def _uses_legacy_test_single_approver(rd: dict) -> bool:
    return bool(rd.get("leave_test_approver") or rd.get("permission_test_approver"))


def _cl_permission_chain(rd: dict) -> bool:
    return bool(rd.get("permission_cl_chain")) or (
        (rd.get("permission_for") or "").strip().lower() == "cl"
        and (rd.get("jmd_route") or "").strip().upper() == "PPC"
    )


def _request_type_label(rd: dict) -> str:
    t = (rd.get("type") or "").strip().upper()
    if t == "VISITOR":
        return "visitor"
    if t == "LEAVE":
        return "leave"
    if t == "PERMISSION":
        return "permission"
    return "OD"


def handle_approval_gate(sender: str, incoming: str) -> bool:
    d = _require()
    resolved = resolve_approval(incoming, sender)
    if resolved[0] is None:
        return False

    is_approve, request_id = resolved
    ref = d.db.collection("requests").document(request_id)
    snap = ref.get()
    if not snap.exists:
        logger.warning("request not found %s (from incoming=%s)", request_id, incoming)
        d.send_to(
            sender,
            "This approval link is invalid or already handled. Check for another pending message.",
        )
        return True

    rd = snap.to_dict()
    employee = rd.get("employee")
    req_label = _request_type_label(rd)
    role = _approval_role(sender, rd)
    if not role:
        if _request_cancelled_by_employee(rd) and _is_single_jmd_approver_sender(
            sender, rd
        ):
            label = _request_type_label(rd)
            d.send_to(
                sender,
                f"This {label} request was already cancelled by the employee.",
            )
            return True
        logger.warning(
            "approval ignored sender=%s request_id=%s jmd=%s md=%s",
            sender,
            request_id,
            rd.get("jmd_status"),
            rd.get("md_status"),
        )
        return True

    if role in ("jmd_i", "jmd_ii"):
        status_field = "jmd_i_status" if role == "jmd_i" else "jmd_ii_status"
        if is_approve:
            ref.update({status_field: "APPROVED"})
            rd = ref.get().to_dict() or {}
            if _dual_jmd_both_approved(rd):
                md_wa = request_md_whatsapp(rd)
                md_off = _md_is_offline_now(d, md_wa)
                ref.update({
                    "jmd_status": "APPROVED",
                    "md_status": _md_status_after_jmd(md_off),
                    "md": md_wa,
                    "manager_status": "N/A",
                    **_md_offline_bypass_fields(d, md_off),
                })
                rd = ref.get().to_dict() or rd
                if md_off:
                    _after_jmd_when_md_offline(
                        d,
                        ref,
                        rd,
                        md_wa,
                        employee=employee,
                        req_label=req_label,
                        request_id=request_id,
                    )
                else:
                    notify_approver(md_wa, rd, request_id)
                    logger.info(
                        "visitor dual JMD approved request_id=%s → md",
                        request_id,
                    )
            else:
                logger.info(
                    "visitor %s approved request_id=%s (awaiting other JMD)",
                    role,
                    request_id,
                )
        else:
            ref.update({
                status_field: "DENIED",
                "jmd_status": "DENIED",
                "md_status": "N/A",
                "manager_status": "N/A",
            })
            d.send_to(employee, f"Your {req_label} request was not approved.")
            logger.info("visitor %s denied request_id=%s", role, request_id)

    elif role == "jmd":
        req_type = (rd.get("type") or "").strip().upper()
        if req_type in ("LEAVE", "PERMISSION") and _uses_legacy_test_single_approver(rd):
            label = "leave" if req_type == "LEAVE" else "permission"
            if is_approve:
                ref.update({
                    "jmd": request_jmd_whatsapp(rd),
                    "jmd_route": (rd.get("jmd_route") or "JMD1").strip().upper(),
                    "manager_status": "N/A",
                    "jmd_status": "APPROVED",
                    "md_status": "N/A",
                    "approved_datetime": d.utcnow(),
                })
                rd_fresh = ref.get().to_dict() or rd
                d.send_to(employee, _employee_final_approval_message(label, rd_fresh))
                logger.info("jmd approved %s (final) request_id=%s", label, request_id)
            else:
                ref.update({
                    "manager_status": "N/A",
                    "jmd_status": "DENIED",
                    "md_status": "N/A",
                })
                d.send_to(employee, f"Your {label} request was not approved.")
                logger.info("jmd denied %s request_id=%s", label, request_id)
        elif is_approve:
            md_wa = request_md_whatsapp(rd)
            if _cl_permission_chain(rd):
                ref.update({
                    "jmd": request_jmd_whatsapp(rd),
                    "jmd_route": (rd.get("jmd_route") or "PPC").strip().upper(),
                    "md": md_wa,
                    "manager_status": "N/A",
                    "jmd_status": "APPROVED",
                    "md_status": "PENDING",
                })
                rd = ref.get().to_dict() or rd
                notify_approver(md_wa, rd, request_id)
                logger.info(
                    "ppc approved CL permission request_id=%s → hr",
                    request_id,
                )
            else:
                md_off = _md_is_offline_now(d, md_wa)
                if req_type == "LEAVE":
                    ref.update({
                        "jmd": request_jmd_whatsapp(rd),
                        "jmd_route": (rd.get("jmd_route") or "JMD1").strip().upper(),
                        "md": md_wa,
                        "manager_status": "N/A",
                        "jmd_status": "APPROVED",
                        "md_status": "PENDING",
                    })
                    rd = ref.get().to_dict() or rd
                    if md_off:
                        d.send_to(
                            employee,
                            "Your leave request was approved by JMD and is pending MD approval.",
                        )
                        logger.info(
                            "jmd approved leave request_id=%s → md pending (md offline)",
                            request_id,
                        )
                    else:
                        notify_approver(md_wa, rd, request_id)
                        logger.info("jmd approved leave request_id=%s → md", request_id)
                else:
                    ref.update({
                        "jmd": request_jmd_whatsapp(rd),
                        "jmd_route": (rd.get("jmd_route") or "JMD1").strip().upper(),
                        "md": md_wa,
                        "manager_status": "N/A",
                        "jmd_status": "APPROVED",
                        "md_status": _md_status_after_jmd(md_off),
                        **_md_offline_bypass_fields(d, md_off),
                    })
                    rd = ref.get().to_dict() or rd
                    if md_off:
                        _after_jmd_when_md_offline(
                            d,
                            ref,
                            rd,
                            md_wa,
                            employee=employee,
                            req_label=req_label,
                            request_id=request_id,
                        )
                    else:
                        notify_approver(md_wa, rd, request_id)
                        logger.info("jmd approved request_id=%s → md", request_id)
        else:
            ref.update({
                "manager_status": "N/A",
                "jmd_status": "DENIED",
                "md_status": "N/A",
            })
            d.send_to(employee, f"Your {req_label} request was not approved.")
            logger.info("jmd denied request_id=%s", request_id)

    elif role == "md":
        if is_approve:
            patch = {
                "md_status": "APPROVED",
                "approved_datetime": d.utcnow(),
            }
            if (rd.get("jmd_status") or "").strip().upper() in (
                "PENDING",
                "AWAITING_MANAGER",
            ):
                patch["jmd_status"] = "APPROVED"
            ref.update(patch)
            if (rd.get("type") or "").strip().upper() == "VISITOR":
                fresh = ref.get()
                rd_fresh = fresh.to_dict() if fresh.exists else rd
                d.on_visitor_md_approved(ref, rd_fresh)
            else:
                fresh = ref.get()
                rd_fresh = fresh.to_dict() if fresh.exists else rd
                d.send_to(
                    employee, _employee_final_approval_message(req_label, rd_fresh)
                )
            logger.info("md approved (final) request_id=%s", request_id)
        else:
            ref.update({
                "manager_status": "N/A",
                "md_status": "DENIED",
            })
            d.send_to(employee, f"Your {req_label} request was not approved.")
            logger.info("md denied (final) request_id=%s", request_id)

    snap = d.session_ref(sender).get()
    if snap.exists:
        data = snap.to_dict() or {}
        if data.get("state") in ("WAITING_APPROVAL_ACTION", WAITING_LEAVE_DAYS_MODIFY):
            if d.db.collection("users").document(sender).get().exists:
                d.session_merge(sender, state=d.menu_idle_state)
            else:
                d.session_ref(sender).delete()

    return True
