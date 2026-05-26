"""
Shared JMD → MD approval (OD, visitor, and future request types).
Configured from main.py after Firestore and env are ready.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from interakt_api import ensure_customer, send_reply_buttons, wa_id_to_phone

logger = logging.getLogger(__name__)


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
    # Production visitor approvers (all VISITOR requests; OD uses jmd_i / jmd_ii / md).
    visitor_jmd_i: str = ""
    visitor_jmd_ii: str = ""
    visitor_md: str = ""
    # Optional: listed employees use test numbers instead (for pilot testing).
    visitor_test_jmd_i: str = ""
    visitor_test_jmd_ii: str = ""
    visitor_test_md: str = ""
    visitor_test_employee_wa_ids: frozenset[str] = frozenset()


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


def _visitor_jmd_for_route(d: ApprovalDeps, jmd_route: str, *, use_test: bool) -> str:
    if use_test and d.visitor_test_jmd_i:
        if (jmd_route or "").strip().upper() == "JMD2" and d.visitor_test_jmd_ii:
            return d.visitor_test_jmd_ii
        return d.visitor_test_jmd_i
    if d.visitor_jmd_i:
        if (jmd_route or "").strip().upper() == "JMD2" and d.visitor_jmd_ii:
            return d.visitor_jmd_ii
        return d.visitor_jmd_i
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
    if (rd.get("type") or "").strip().upper() == "VISITOR" and d.visitor_md:
        return d.visitor_md
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
) -> dict | None:
    if not user_data:
        return None
    d = _require()
    jmd_route = (user_data.get("jmd_route") or "JMD1").strip().upper()
    req_type = (request_type or "OD").strip().upper()

    if req_type == "VISITOR":
        use_test = _use_visitor_test_approvers(employee_wa)
        jmd = _visitor_jmd_for_route(d, jmd_route, use_test=use_test)
        if use_test and d.visitor_test_md:
            md = d.visitor_test_md
        elif d.visitor_md:
            md = d.visitor_md
        else:
            md = ""
        if not jmd or not md:
            logger.error(
                "visitor approvers not configured (set VISITOR_JMD_* and VISITOR_MD, "
                "or VISITOR_TEST_* for pilot employees)"
            )
            return None
        chain = {"jmd": jmd, "jmd_route": jmd_route, "md": md, "visitor_approval": True}
        if use_test:
            chain["approval_test"] = True
        return chain

    jmd = jmd_whatsapp_for_route(jmd_route, for_request_type="OD")
    md = d.md
    if not jmd or not md:
        return None
    return {"jmd": jmd, "jmd_route": jmd_route, "md": md}


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
        names = ", ".join(rd.get("visitor_names") or []) or "—"
        coming_from = (
            (rd.get("coming_from") or rd.get("coming_from_label") or rd.get("organization") or "")
            .strip()
            or "—"
        )
        coming_for = (
            (rd.get("coming_for_label") or rd.get("visit_for_label") or "").strip()
            or "—"
        )
        test_tag = "[TEST] " if rd.get("approval_test") else ""
        return (
            f"{test_tag}Visitor approval request\n\n"
            f"Employee: {emp}\n"
            f"Department: {dept}\n"
            f"People: {rd.get('people_count') or '—'}\n"
            f"Names: {names}\n"
            f"Coming from: {coming_from}\n"
            f"Coming for: {coming_for}\n"
            f"Guest WhatsApp: {rd.get('guest_phone') or '—'}\n\n"
            "Please approve or deny."
        )
    return (
        "OD approval request\n\n"
        f"Employee: {emp}\n"
        f"Department: {dept}\n"
        f"Reason: {reason or '—'}\n\n"
        "Please approve or deny."
    )


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
    approve_id = f"APPROVE_{rid}"[:256]
    deny_id = f"DENY_{rid}"[:256]
    body = _approval_message_body(
        employee_name=employee_name,
        department=department,
        reason=reason,
        request_rd=request_rd,
    )
    try:
        send_reply_buttons(
            wa_id_to_phone(wa_id),
            body,
            [(approve_id, "Approve"), (deny_id, "Deny")],
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
                [(approve_id, "Approve"), (deny_id, "Deny")],
                callback_data=request_id,
            )
            return True
        except Exception:
            logger.exception("approval retry failed to=%s", wa_id)
        return False


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
        rid = raw[8:].strip()
        return (True, rid) if rid else (None, None)
    if upper.startswith("DENY_"):
        rid = raw[4:].strip()
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
    jmd_wa = request_jmd_whatsapp(rd)

    if d.same_whatsapp(sender, jmd_wa) and jmd_st in ("PENDING", "AWAITING_MANAGER"):
        return "jmd"

    md_wa = request_md_whatsapp(rd)
    if d.same_whatsapp(sender, md_wa) and jmd_st == "APPROVED" and md_st == "PENDING":
        return "md"

    return None


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


def _request_type_label(rd: dict) -> str:
    if (rd.get("type") or "").strip().upper() == "VISITOR":
        return "visitor"
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
        logger.warning(
            "approval ignored sender=%s request_id=%s jmd=%s md=%s",
            sender,
            request_id,
            rd.get("jmd_status"),
            rd.get("md_status"),
        )
        return True

    if role == "jmd":
        if is_approve:
            md_wa = request_md_whatsapp(rd)
            ref.update({
                "jmd": request_jmd_whatsapp(rd),
                "jmd_route": (rd.get("jmd_route") or "JMD1").strip().upper(),
                "md": md_wa,
                "manager_status": "N/A",
                "jmd_status": "APPROVED",
                "md_status": "PENDING",
            })
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
                d.on_visitor_md_approved(ref, rd)
            else:
                d.send_to(employee, "Your OD has been Approved.")
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
        if data.get("state") == "WAITING_APPROVAL_ACTION":
            if d.db.collection("users").document(sender).get().exists:
                d.session_merge(sender, state=d.menu_idle_state)
            else:
                d.session_ref(sender).delete()

    return True
