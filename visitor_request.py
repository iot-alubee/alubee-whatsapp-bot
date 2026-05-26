"""
Visitor request — count (1–5), comma-separated names, coming from (text),
coming for (Customer Visit / Technical Work / Other), guest WhatsApp; JMD → MD; OTP.
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
VISITOR_MAX_PEOPLE = 5

VISITOR_COUNT = "VISITOR_COUNT"
VISITOR_NAMES = "VISITOR_NAMES"
VISITOR_COMING_FROM = "VISITOR_COMING_FROM"
VISITOR_COMING_FOR = "VISITOR_COMING_FOR"
VISITOR_GUEST_PHONE = "VISITOR_GUEST_PHONE"
VISITOR_CONFIRM = "VISITOR_CONFIRM"

VISITOR_STATES = frozenset({
    VISITOR_COUNT,
    VISITOR_NAMES,
    VISITOR_COMING_FROM,
    VISITOR_COMING_FOR,
    VISITOR_GUEST_PHONE,
    VISITOR_CONFIRM,
})

COMING_FOR_CUSTOMER = "CUSTOMER_VISIT"
COMING_FOR_TECHNICAL = "TECHNICAL_WORK"
COMING_FOR_OTHER = "OTHER"

COMING_FOR_LABELS = {
    COMING_FOR_CUSTOMER: "Customer Visit",
    COMING_FOR_TECHNICAL: "Technical Work",
    COMING_FOR_OTHER: "Other",
}


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


def _coming_from_label(rd: dict) -> str:
    return (
        (rd.get("coming_from") or rd.get("coming_from_label") or rd.get("organization") or "")
        .strip()
        or "—"
    )


def _coming_for_label(rd: dict) -> str:
    label = (rd.get("coming_for_label") or rd.get("visit_for_label") or "").strip()
    if label:
        return label
    code = (rd.get("coming_for") or rd.get("visit_for") or "").strip().upper()
    return COMING_FOR_LABELS.get(code, code or "—")


def send_otps_after_md_approve(ref, rd: dict, send_to: Callable[[str, str], None]) -> str:
    """OTP to employee (session) and guest (template) after MD approval."""
    otp = f"{secrets.randbelow(1_000_000):06d}"
    ref.update({"visitor_otp": otp})
    names = ", ".join(rd.get("visitor_names") or []) or "—"
    coming_from = _coming_from_label(rd)
    coming_for = _coming_for_label(rd)
    employee = rd.get("employee")
    guest_wa = (rd.get("guest_whatsapp") or "").strip()
    if not guest_wa and rd.get("guest_phone"):
        d = digits(str(rd.get("guest_phone")))
        if len(d) >= 10:
            guest_wa = wa_from_10(d[-10:])

    send_to(
        employee,
        (
            "Your visitor request is approved.\n\n"
            f"Visitors: {names}\n"
            f"Coming from: {coming_from}\n"
            f"Coming for: {coming_for}\n"
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
            organization=coming_from,
        )
        if not guest_ok:
            send_to(
                employee,
                (
                    f"Visitor OTP {otp} could not be sent on WhatsApp to {guest_phone}. "
                    "Share the OTP with the guest manually."
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
        coming_from="",
        coming_for="",
        coming_for_label="",
        visitor_names=[],
        guest_phone="",
    )
    _send_count_picker(sender, deps)


def handle(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    state = (session or {}).get("state")
    um = (incoming or "").strip().upper()

    if um in ("CANCEL",):
        deps.clear_session(sender)
        deps.go_main_menu(sender)
        return

    if state == VISITOR_COUNT:
        _handle_count(sender, incoming, session, deps)
        return
    if state == VISITOR_NAMES:
        _handle_names(sender, incoming, session, deps)
        return
    if state == VISITOR_COMING_FROM:
        _handle_coming_from(sender, incoming, session, deps)
        return
    if state == VISITOR_COMING_FOR:
        _handle_coming_for(sender, incoming, session, deps)
        return
    if state == VISITOR_GUEST_PHONE:
        _handle_guest_phone(sender, incoming, session, deps)
        return
    if state == VISITOR_CONFIRM:
        _handle_confirm(sender, incoming, session, deps)
        return

    deps.send_to(sender, "Follow the visitor request steps, or send CANCEL.")


def _people_count(session: dict) -> int:
    try:
        n = int(session.get("people_count") or VISITOR_MIN_PEOPLE)
    except (TypeError, ValueError):
        n = VISITOR_MIN_PEOPLE
    return max(VISITOR_MIN_PEOPLE, min(VISITOR_MAX_PEOPLE, n))


def _parse_count_choice(incoming: str) -> int | None:
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
        return None
    if VISITOR_MIN_PEOPLE <= n <= VISITOR_MAX_PEOPLE:
        return n
    return None


def _parse_coming_for_choice(incoming: str) -> str | None:
    key = (incoming or "").strip().lower().replace(" ", "_")
    mapping = {
        "customer_visit": COMING_FOR_CUSTOMER,
        "visitor_coming_for_customer": COMING_FOR_CUSTOMER,
        "visitor_coming_for_customer_visit": COMING_FOR_CUSTOMER,
        "customer": COMING_FOR_CUSTOMER,
        "technical_work": COMING_FOR_TECHNICAL,
        "visitor_coming_for_technical": COMING_FOR_TECHNICAL,
        "technical": COMING_FOR_TECHNICAL,
        "other": COMING_FOR_OTHER,
        "visitor_coming_for_other": COMING_FOR_OTHER,
    }
    if key in mapping:
        return mapping[key]
    um = (incoming or "").strip().upper()
    if um in (COMING_FOR_CUSTOMER, COMING_FOR_TECHNICAL, COMING_FOR_OTHER):
        return um
    return None


def _parse_comma_names(text: str, expected: int) -> tuple[list[str] | None, str]:
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if not parts:
        return None, "Enter visitor names separated by commas."
    valid = [p for p in parts if len(p) >= 2]
    if len(valid) < len(parts):
        return None, "Each name must be at least 2 characters."
    if len(valid) != expected:
        return (
            None,
            f"You selected {expected} visitor(s). Enter exactly {expected} names, "
            f"separated by commas (you entered {len(valid)}).",
        )
    return valid, ""


def _send_count_picker(sender: str, deps: VisitorDeps) -> None:
    rows = [
        {"id": f"visitor_count_{n}", "title": f"{n} visitor{'s' if n > 1 else ''}"[:24]}
        for n in range(VISITOR_MIN_PEOPLE, VISITOR_MAX_PEOPLE + 1)
    ]
    rows.append({"id": "back", "title": "Back"})
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            "Visitor request\n\nHow many people?",
            rows,
            button_label="Select count",
            section_title="Count",
            callback_data="visitor-count",
        )
    except Exception:
        logger.exception("visitor count list failed")
        deps.send_to(sender, "How many people? Reply 1 to 5, or BACK.")


def _send_coming_for_picker(sender: str, deps: VisitorDeps) -> None:
    rows = [
        {"id": "visitor_coming_for_customer", "title": "Customer Visit"},
        {"id": "visitor_coming_for_technical", "title": "Technical Work"},
        {"id": "visitor_coming_for_other", "title": "Other"},
        {"id": "back", "title": "Back"},
    ]
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            "Coming for?",
            rows,
            button_label="Select purpose",
            section_title="Coming for",
            callback_data="visitor-coming-for",
        )
    except Exception:
        logger.exception("visitor coming-for list failed")
        deps.send_to(
            sender,
            "Coming for?\nReply CUSTOMER VISIT, TECHNICAL WORK, or OTHER, or BACK.",
        )


def _prompt_comma_names(sender: str, count: int, deps: VisitorDeps) -> None:
    example = ", ".join(f"Name{i}" for i in range(1, min(count + 1, 4)))
    if count > 3:
        example += ", ..."
    deps.send_to(
        sender,
        f"Enter all {count} visitor name(s) in one message, separated by commas.\n"
        f"Example ({count} people): {example}",
    )


def _handle_count(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.clear_session(sender)
        deps.go_main_menu(sender)
        return

    count = _parse_count_choice(incoming)
    if count is None:
        deps.send_to(sender, "Please select 1 to 5 from the list.")
        _send_count_picker(sender, deps)
        return

    deps.session_merge(sender, state=VISITOR_NAMES, people_count=count, visitor_names=[])
    _prompt_comma_names(sender, count, deps)


def _handle_names(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.session_merge(sender, state=VISITOR_COUNT)
        _send_count_picker(sender, deps)
        return

    count = _people_count(session)
    names, err = _parse_comma_names(incoming, count)
    if names is None:
        deps.send_to(sender, err)
        _prompt_comma_names(sender, count, deps)
        return

    deps.session_merge(sender, state=VISITOR_COMING_FROM, visitor_names=names, coming_from="")
    deps.send_to(
        sender,
        "Coming from?\n"
        "Enter where the visitor(s) are coming from (company or place). Reply with text, or BACK.",
    )


def _handle_coming_from(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.session_merge(sender, state=VISITOR_NAMES)
        _prompt_comma_names(sender, _people_count(session), deps)
        return

    detail = (incoming or "").strip()
    if len(detail) < 2:
        deps.send_to(sender, "Please enter where they are coming from (at least 2 characters).")
        return

    deps.session_merge(
        sender,
        state=VISITOR_COMING_FOR,
        coming_from=detail,
        coming_for="",
        coming_for_label="",
    )
    _send_coming_for_picker(sender, deps)


def _handle_coming_for(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.session_merge(sender, state=VISITOR_COMING_FROM)
        deps.send_to(
            sender,
            "Coming from?\nEnter where the visitor(s) are coming from. Reply with text, or BACK.",
        )
        return

    choice = _parse_coming_for_choice(incoming)
    if not choice:
        deps.send_to(sender, "Select Customer Visit, Technical Work, or Other from the list.")
        _send_coming_for_picker(sender, deps)
        return

    label = COMING_FOR_LABELS[choice]
    deps.session_merge(
        sender,
        state=VISITOR_GUEST_PHONE,
        coming_for=choice,
        coming_for_label=label,
    )
    deps.send_to(
        sender,
        "Guest WhatsApp number\n\n"
        "Enter the visitor's WhatsApp number (10-digit Indian mobile, e.g. 9876543210).\n"
        "After approval, the OTP will be sent to this number and to you.",
    )


def _handle_guest_phone(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.session_merge(sender, state=VISITOR_COMING_FOR, guest_phone="")
        _send_coming_for_picker(sender, deps)
        return

    d = digits(incoming)
    if len(d) < 10:
        deps.send_to(sender, "Please enter a valid 10-digit WhatsApp number.")
        return

    phone10 = d[-10:]
    deps.session_merge(sender, state=VISITOR_CONFIRM, guest_phone=phone10)
    _show_confirm(sender, session, deps, guest_phone=phone10)


def _show_confirm(sender: str, session: dict, deps: VisitorDeps, **updates) -> None:
    data = {**session, **updates}
    count = _people_count(data)
    names = data.get("visitor_names") or []
    coming_from = (data.get("coming_from") or "").strip() or "—"
    coming_for = (data.get("coming_for_label") or "").strip() or "—"
    guest = data.get("guest_phone") or ""

    body = (
        "Please confirm visitor request:\n\n"
        f"People: {count}\n"
        f"Names: {', '.join(names)}\n"
        f"Coming from: {coming_from}\n"
        f"Coming for: {coming_for}\n"
        f"Guest WhatsApp: {guest}\n"
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


def _build_summary(
    names: list,
    count: int,
    guest_phone: str,
    coming_from: str,
    coming_for: str,
) -> str:
    name_str = ", ".join(names) if names else "—"
    return (
        f"People: {count} | {name_str} | From: {coming_from} | "
        f"For: {coming_for} | Guest WhatsApp: {guest_phone}"
    )


def _handle_confirm(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.session_merge(sender, state=VISITOR_GUEST_PHONE)
        deps.send_to(
            sender,
            "Enter the guest WhatsApp number (10 digits), or BACK.",
        )
        return
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
    coming_from = (session.get("coming_from") or "").strip()
    coming_for = (session.get("coming_for") or "").strip()
    coming_for_label = (session.get("coming_for_label") or COMING_FOR_LABELS.get(coming_for, "")).strip()
    guest_wa = wa_from_10(guest_phone)
    summary = _build_summary(names, count, guest_phone, coming_from, coming_for_label)

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
        "coming_from": coming_from,
        "coming_from_label": coming_from,
        "coming_for": coming_for,
        "coming_for_label": coming_for_label,
        "visit_for": coming_for,
        "visit_for_label": coming_for_label,
        "organization": coming_from,
        "guest_phone": guest_phone,
        "guest_whatsapp": guest_wa,
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
    logger.info("VISITOR created %s jmd_route=%s", request_id, chain["jmd_route"])

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
