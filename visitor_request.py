"""
Visitor request flow — headcount, names, guest phone, organization; JMD → MD; OTP on MD approve.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Callable

from interakt_api import (
    send_guest_visit_otp,
    send_list_menu,
    send_reply_buttons,
    wa_id_to_phone,
)

from bot_shared import digits, find_open_request, wa_from_10

logger = logging.getLogger(__name__)

VISITOR_MIN_PEOPLE = 1
VISITOR_MAX_PEOPLE = 10

VISITOR_COUNT = "VISITOR_COUNT"
VISITOR_NAME = "VISITOR_NAME"
VISITOR_GUEST_PHONE = "VISITOR_GUEST_PHONE"
VISITOR_ORGANIZATION = "VISITOR_ORGANIZATION"
VISITOR_CONFIRM = "VISITOR_CONFIRM"

VISITOR_STATES = frozenset({
    VISITOR_COUNT,
    VISITOR_NAME,
    VISITOR_GUEST_PHONE,
    VISITOR_ORGANIZATION,
    VISITOR_CONFIRM,
})

@dataclass
class VisitorDeps:
    db: object
    send_to: Callable[[str, str], None]
    session_merge: Callable[..., None]
    session_ref: Callable[[str], object]
    utcnow: Callable
    build_approval_chain: Callable[[dict, str], dict | None]
    notify_jmd: Callable[[str, dict, str], bool]
    clear_session: Callable[[str], None]
    go_main_menu: Callable[[str], None]
    already_pending_msg: str


def is_visitor_state(state: str | None) -> bool:
    return (state or "") in VISITOR_STATES


def send_otps_after_md_approve(ref, rd: dict, send_to: Callable[[str, str], None]) -> str:
    """Called from approval.py when MD approves a visitor request."""
    otp = f"{secrets.randbelow(1_000_000):06d}"
    ref.update({"visitor_otp": otp})
    names = ", ".join(rd.get("visitor_names") or []) or "—"
    org = (rd.get("organization") or "").strip() or "—"
    employee = rd.get("employee")
    guest_wa = (rd.get("guest_whatsapp") or "").strip()
    if not guest_wa and rd.get("guest_phone"):
        d = digits(str(rd.get("guest_phone")))
        if len(d) >= 10:
            guest_wa = wa_from_10(d[-10:])

    send_to(
        employee,
        (
            "Your visitor request has been approved.\n\n"
            f"Visitors: {names}\n"
            f"From: {org}\n"
            f"Entry OTP: {otp}\n\n"
            "Share this OTP with your visitors and security at the gate."
        ),
    )

    if guest_wa:
        guest_name = (rd.get("visitor_names") or ["Guest"])[0]
        guest_phone = wa_id_to_phone(guest_wa)
        guest_ok = send_guest_visit_otp(
            guest_phone,
            guest_name=str(guest_name)[:50],
            otp=otp,
            organization=org,
        )
        if not guest_ok:
            send_to(
                employee,
                (
                    f"Visitor OTP {otp} could not be sent on WhatsApp to {guest_phone}. "
                    "Share the OTP with the guest manually. "
                    "(Set VISITOR_OTP_TEMPLATE_NAME in Interakt for automatic guest messages.)"
                ),
            )
    return otp


def try_start(sender: str, deps: VisitorDeps) -> None:
    if find_open_request(sender, "VISITOR"):
        deps.send_to(sender, deps.already_pending_msg)
        return
    deps.session_merge(
        sender,
        state=VISITOR_COUNT,
        people_count=0,
        visitor_names=[],
        visitor_name_index=0,
        guest_phone="",
        organization="",
    )
    _send_count_picker(sender, deps)


def handle(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    state = (session or {}).get("state")
    um = (incoming or "").strip().upper()

    if um in ("BACK", "CANCEL"):
        deps.clear_session(sender)
        deps.go_main_menu(sender)
        return

    if state == VISITOR_COUNT:
        _handle_count(sender, incoming, session, deps)
        return
    if state == VISITOR_NAME:
        _handle_name(sender, incoming, session, deps)
        return
    if state == VISITOR_GUEST_PHONE:
        _handle_guest_phone(sender, incoming, session, deps)
        return
    if state == VISITOR_ORGANIZATION:
        _handle_organization(sender, incoming, session, deps)
        return
    if state == VISITOR_CONFIRM:
        _handle_confirm(sender, incoming, session, deps)
        return

    deps.send_to(sender, "Use the list or reply 1–10 for visitor count, or BACK to cancel.")


def _parse_count_choice(incoming: str) -> int | None:
    """List row visitor_count_N, digit N, or legacy +/- flow."""
    raw = (incoming or "").strip()
    if not raw:
        return None
    key = raw.lower().replace(" ", "_")
    if key.startswith("visitor_count_"):
        try:
            n = int(key.split("_")[-1])
        except ValueError:
            return None
    elif raw.isdigit():
        n = int(raw)
    else:
        um = raw.upper()
        if um.startswith("VISITOR_COUNT_"):
            try:
                n = int(um.split("_")[-1])
            except ValueError:
                return None
        else:
            return None
    if VISITOR_MIN_PEOPLE <= n <= VISITOR_MAX_PEOPLE:
        return n
    return None


def _people_count(session: dict) -> int:
    try:
        n = int(session.get("people_count") or VISITOR_MIN_PEOPLE)
    except (TypeError, ValueError):
        n = VISITOR_MIN_PEOPLE
    return max(VISITOR_MIN_PEOPLE, min(VISITOR_MAX_PEOPLE, n))


def _send_count_picker(sender: str, deps: VisitorDeps) -> None:
    """InteractiveList: pick 1–10 visitors in one tap (same pattern as main menu / OD reason)."""
    rows = [
        {"id": f"visitor_count_{n}", "title": f"{n} visitor{'s' if n > 1 else ''}"[:24]}
        for n in range(VISITOR_MIN_PEOPLE, VISITOR_MAX_PEOPLE + 1)
    ]
    rows.append({"id": "back", "title": "Back"})
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            "Visitor request\n\nHow many visitors?",
            rows,
            button_label="Select count",
            section_title="Visitors",
            callback_data="visitor-count",
        )
    except Exception:
        logger.exception("visitor count list failed")
        deps.send_to(
            sender,
            "How many visitors?\nReply with a number from 1 to 10, or BACK.",
        )


def _handle_count(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.clear_session(sender)
        deps.go_main_menu(sender)
        return

    count = _parse_count_choice(incoming)
    if count is None:
        deps.send_to(sender, "Please select 1–10 from the list, or type a number from 1 to 10.")
        _send_count_picker(sender, deps)
        return

    deps.session_merge(
        sender,
        state=VISITOR_NAME,
        people_count=count,
        visitor_names=[],
        visitor_name_index=0,
    )
    _prompt_name(sender, 1, count, deps)


def _prompt_name(sender: str, index: int, total: int, deps: VisitorDeps) -> None:
    deps.send_to(
        sender,
        f"Enter full name of visitor {index} of {total} (reply with text):",
    )


def _handle_name(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    name = (incoming or "").strip()
    if len(name) < 2:
        deps.send_to(sender, "Please enter a valid name (at least 2 characters).")
        return

    total = _people_count(session)
    names = list(session.get("visitor_names") or [])
    names.append(name)
    idx = len(names)

    if idx < total:
        deps.session_merge(sender, visitor_names=names, visitor_name_index=idx)
        _prompt_name(sender, idx + 1, total, deps)
        return

    deps.session_merge(sender, state=VISITOR_GUEST_PHONE, visitor_names=names)
    deps.send_to(
        sender,
        "Enter the WhatsApp phone number of any one guest "
        "(10-digit Indian mobile, e.g. 9876543210).\n"
        "The visit OTP will be sent to this number on WhatsApp.",
    )


def _handle_guest_phone(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    d = digits(incoming)
    if len(d) < 10:
        deps.send_to(
            sender,
            "Please enter a valid 10-digit WhatsApp number for the guest.",
        )
        return
    phone10 = d[-10:]
    deps.session_merge(sender, state=VISITOR_ORGANIZATION, guest_phone=phone10)
    deps.send_to(
        sender,
        "Organization / where are they coming from?\n(Reply with text)",
    )


def _handle_organization(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    org = (incoming or "").strip()
    if len(org) < 2:
        deps.send_to(sender, "Please enter organization or place of visit.")
        return

    names = session.get("visitor_names") or []
    count = _people_count(session)
    guest = session.get("guest_phone") or ""

    deps.session_merge(sender, state=VISITOR_CONFIRM, organization=org)
    body = (
        "Please confirm visitor request:\n\n"
        f"Visitors: {count}\n"
        f"Names: {', '.join(names)}\n"
        f"Guest WhatsApp: {guest}\n"
        f"Organization: {org}\n"
    )
    try:
        send_reply_buttons(
            wa_id_to_phone(sender),
            body,
            [("SUBMIT", "Submit"), ("CANCEL", "Cancel"), ("BACK", "Back")],
            callback_data="visitor-confirm",
        )
    except Exception:
        deps.send_to(sender, f"{body}\n\nReply SUBMIT to send, or CANCEL.")


def _build_summary(names: list, count: int, guest_phone: str, organization: str) -> str:
    name_str = ", ".join(names) if names else "—"
    return (
        f"Visitors: {count} | {name_str} | Guest WhatsApp: {guest_phone} | From: {organization}"
    )


def _handle_confirm(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um != "SUBMIT":
        deps.send_to(sender, "Reply SUBMIT to send the request, or CANCEL.")
        return
    _submit(sender, session, deps)


def _submit(sender: str, session: dict, deps: VisitorDeps) -> None:
    if find_open_request(sender, "VISITOR"):
        deps.clear_session(sender)
        deps.send_to(sender, deps.already_pending_msg)
        return

    user_doc = deps.db.collection("users").document(sender).get()
    if not user_doc.exists:
        deps.clear_session(sender)
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return

    ud = user_doc.to_dict()
    chain = deps.build_approval_chain(ud, sender)
    if not chain:
        deps.clear_session(sender)
        deps.send_to(sender, "Approval chain not configured.\nPlease contact admin.")
        return

    names = list(session.get("visitor_names") or [])
    count = _people_count(session)
    guest_phone = (session.get("guest_phone") or "").strip()
    organization = (session.get("organization") or "").strip()
    guest_wa = wa_from_10(guest_phone)
    summary = _build_summary(names, count, guest_phone, organization)

    ref = deps.db.collection("requests").document()
    request_id = ref.id
    payload = {
        "request_id": request_id,
        "requested_datetime": deps.utcnow(),
        "employee": sender,
        "employee_id": ud.get("employee_id") or "",
        "employee_name": ud.get("name") or "Employee",
        "department": ud.get("department") or "",
        "type": "VISITOR",
        "reason": summary,
        "people_count": count,
        "visitor_names": names,
        "guest_phone": guest_phone,
        "guest_whatsapp": guest_wa,
        "organization": organization,
        "jmd": chain["jmd"],
        "jmd_route": chain["jmd_route"],
        "md": chain["md"],
        "manager_status": "N/A",
        "jmd_status": "PENDING",
        "md_status": "AWAITING_JMD",
        "visitor_otp": "",
    }
    if chain.get("approval_test"):
        payload["approval_test"] = True
    ref.set(payload)
    logger.info(
        "VISITOR created %s jmd_route=%s approval_test=%s",
        request_id,
        chain["jmd_route"],
        bool(chain.get("approval_test")),
    )

    rd = ref.get().to_dict()
    jmd_ok = deps.notify_jmd(chain["jmd"], rd, request_id)

    deps.clear_session(sender)
    msg = "Visitor request is submitted."
    if chain.get("approval_test"):
        msg += " (pilot test JMD/MD — OD approvers unchanged)."
    if not jmd_ok:
        route = chain["jmd_route"]
        msg += (
            f"\n\nJMD ({route}) could not be notified on WhatsApp. "
            "Ask them to send Hi to this Alubee number once, then contact admin."
        )
    deps.send_to(sender, msg)
