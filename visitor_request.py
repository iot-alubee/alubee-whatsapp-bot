"""
Visitor request flow:
Coming On date -> Coming From -> Purpose (Customer Visit / Other) -> if Other, text purpose ->
Visiting to (Unit I / Unit II / Both) -> No of people -> Visitor name(s) -> Visitor mobile ->
Confirm -> JMD(s) -> MD -> OTP.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from dataclasses import dataclass
from typing import Callable

from interakt_api import (
    send_guest_visit_otp,
    send_list_menu,
    send_reply_buttons,
    wa_id_to_phone,
)

from approval import (
    VISITING_BOTH,
    VISITING_TO_LABELS,
    VISITING_UNIT_I,
    VISITING_UNIT_II,
    visitor_chain_failure_message,
)

from bot_shared import digits, find_open_request, wa_from_10

logger = logging.getLogger(__name__)

VISITOR_MIN_PEOPLE = 1
VISITOR_MAX_PEOPLE = 50

VISITOR_COMING_ON = "VISITOR_COMING_ON"
VISITOR_COMING_FROM = "VISITOR_COMING_FROM"
VISITOR_PURPOSE = "VISITOR_PURPOSE"
VISITOR_PURPOSE_OTHER = "VISITOR_PURPOSE_OTHER"
VISITOR_VISITING_TO = "VISITOR_VISITING_TO"
VISITOR_COUNT = "VISITOR_COUNT"
VISITOR_NAMES = "VISITOR_NAMES"
VISITOR_GUEST_PHONE = "VISITOR_GUEST_PHONE"
VISITOR_CONFIRM = "VISITOR_CONFIRM"

VISITOR_STATES = frozenset({
    VISITOR_COMING_ON,
    VISITOR_COMING_FROM,
    VISITOR_PURPOSE,
    VISITOR_PURPOSE_OTHER,
    VISITOR_VISITING_TO,
    VISITOR_COUNT,
    VISITOR_NAMES,
    VISITOR_GUEST_PHONE,
    VISITOR_CONFIRM,
})

PURPOSE_CUSTOMER = "CUSTOMER_VISIT"
PURPOSE_OTHER = "OTHER"

PURPOSE_LABELS = {
    PURPOSE_CUSTOMER: "Customer Visit",
    PURPOSE_OTHER: "Other",
}


@dataclass
class VisitorDeps:
    db: object
    send_to: Callable[[str, str], None]
    session_merge: Callable[..., None]
    session_ref: Callable[[str], object]
    utcnow: Callable
    build_approval_chain: Callable[..., dict | None]
    notify_visitor_on_submit: Callable[[dict, str, dict], bool]
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
    label = (
        rd.get("purpose_label")
        or rd.get("coming_for_label")
        or rd.get("visit_for_label")
        or ""
    ).strip()
    if label:
        return label
    code = (
        rd.get("purpose")
        or rd.get("coming_for")
        or rd.get("visit_for")
        or ""
    ).strip().upper()
    return PURPOSE_LABELS.get(code, code or "—")


def _coming_on_label(rd: dict) -> str:
    return (rd.get("coming_on_date") or rd.get("visit_date") or "").strip() or "—"


def _resolve_guest_contact(rd: dict) -> tuple[str, str]:
    """Return (whatsapp_wa_id, 10-digit phone) for the visitor guest."""
    for key in ("guest_whatsapp", "guest_wa", "guest_mobile", "visitor_mobile"):
        raw = (rd.get(key) or "").strip()
        if not raw:
            continue
        d = digits(raw)
        if len(d) >= 10:
            phone10 = d[-10:]
            return wa_from_10(phone10), phone10
    raw = (rd.get("guest_phone") or "").strip()
    d = digits(str(raw))
    if len(d) >= 10:
        phone10 = d[-10:]
        return wa_from_10(phone10), phone10
    return "", ""


def send_otps_after_md_approve(ref, rd: dict, send_to: Callable[[str, str], None]) -> str:
    """OTP to employee (session) and guest (template) after MD approval."""
    snap = ref.get()
    if snap.exists:
        rd = snap.to_dict() or rd

    otp = f"{secrets.randbelow(1_000_000):06d}"
    ref.update({"visitor_otp": otp, "guest_otp_sent": False})

    names = ", ".join(rd.get("visitor_names") or []) or "—"
    coming_on = _coming_on_label(rd)
    coming_from = _coming_from_label(rd)
    coming_for = _coming_for_label(rd)
    employee = rd.get("employee")
    guest_wa, guest_phone10 = _resolve_guest_contact(rd)
    request_id = (rd.get("request_id") or "").strip()

    send_to(
        employee,
        (
            "Your visitor request is approved.\n\n"
            f"Coming on: {coming_on}\n"
            f"Visitors: {names}\n"
            f"Coming from: {coming_from}\n"
            f"Coming for: {coming_for}\n"
            f"Entry OTP: {otp}\n\n"
            "Share this OTP with your visitors and security at the gate."
        ),
    )

    if not guest_phone10:
        logger.warning(
            "visitor OTP: no guest phone on request_id=%s keys=%s",
            request_id,
            [k for k in ("guest_phone", "guest_whatsapp") if rd.get(k)],
        )
        send_to(
            employee,
            "Visitor WhatsApp number was not saved on this request. "
            "Share the OTP with the guest manually.",
        )
        return otp

    raw_names = rd.get("visitor_names") or []
    if isinstance(raw_names, str):
        guest_name = raw_names.split(",")[0].strip() or "Guest"
    else:
        guest_name = (raw_names[0] if raw_names else "Guest") or "Guest"

    guest_ok = send_guest_visit_otp(
        guest_phone10,
        guest_name=str(guest_name)[:50],
        otp=otp,
        organization=coming_from,
    )
    if guest_ok:
        ref.update({"guest_otp_sent": True, "guest_phone": guest_phone10})
        send_to(
            employee,
            f"Entry OTP was also sent on WhatsApp to the visitor ({guest_phone10}).",
        )
    else:
        send_to(
            employee,
            (
                f"Visitor OTP {otp} could not be sent on WhatsApp to {guest_phone10}. "
                "Ask the guest to send Hi to this Alubee number once, or share the OTP manually."
            ),
        )
    return otp


def visitor_flow_enabled() -> bool:
    return False


def try_start(sender: str, deps: VisitorDeps) -> None:
    """Start visitor request in chat (message-by-message)."""
    _try_start_chat(sender, deps)


def _try_start_chat(sender: str, deps: VisitorDeps) -> None:
    if find_open_request(sender, "VISITOR"):
        deps.send_to(sender, deps.already_pending_msg)
        return
    deps.session_merge(
        sender,
        state=VISITOR_COMING_ON,
        coming_on_date="",
        people_count=0,
        coming_from="",
        purpose="",
        purpose_label="",
        purpose_detail="",
        coming_for="",
        coming_for_label="",  # compatibility with existing consumers
        visiting_to="",
        visiting_to_label="",
        visitor_names=[],
        guest_phone="",
    )
    deps.send_to(
        sender,
        "Visitor form\n\nComing On (DD-MM-YYYY)?",
    )


def handle(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    state = (session or {}).get("state")
    um = (incoming or "").strip().upper()

    if um in ("CANCEL",):
        deps.clear_session(sender)
        deps.go_main_menu(sender)
        return

    if state == VISITOR_COMING_ON:
        _handle_coming_on(sender, incoming, session, deps)
        return
    if state == VISITOR_COMING_FROM:
        _handle_coming_from(sender, incoming, session, deps)
        return
    if state == VISITOR_PURPOSE:
        _handle_purpose(sender, incoming, session, deps)
        return
    if state == VISITOR_PURPOSE_OTHER:
        _handle_purpose_other(sender, incoming, session, deps)
        return
    if state == VISITOR_VISITING_TO:
        _handle_visiting_to(sender, incoming, session, deps)
        return
    if state == VISITOR_COUNT:
        _handle_count(sender, incoming, session, deps)
        return
    if state == VISITOR_NAMES:
        _handle_names(sender, incoming, session, deps)
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
    if not raw.isdigit():
        return None
    n = int(raw)
    if VISITOR_MIN_PEOPLE <= n <= VISITOR_MAX_PEOPLE:
        return n
    return None


def _visiting_to_label(code: str) -> str:
    return VISITING_TO_LABELS.get((code or "").strip().upper(), code or "—")


def _parse_visiting_to_choice(incoming: str) -> str | None:
    key = (incoming or "").strip().lower().replace(" ", "_")
    mapping = {
        "visitor_visit_unit_i": VISITING_UNIT_I,
        "visitor_visit_unit_ii": VISITING_UNIT_II,
        "visitor_visit_both": VISITING_BOTH,
        "unit_i": VISITING_UNIT_I,
        "unit_ii": VISITING_UNIT_II,
        "unit_1": VISITING_UNIT_I,
        "unit_2": VISITING_UNIT_II,
        "both": VISITING_BOTH,
    }
    if key in mapping:
        return mapping[key]
    um = (incoming or "").strip().upper()
    if um in (VISITING_UNIT_I, VISITING_UNIT_II, VISITING_BOTH):
        return um
    return None


def _send_visiting_to_picker(sender: str, deps: VisitorDeps) -> None:
    try:
        send_reply_buttons(
            wa_id_to_phone(sender),
            "Visiting to?",
            [
                ("visitor_visit_unit_i", "Unit I"),
                ("visitor_visit_unit_ii", "Unit II"),
                ("visitor_visit_both", "Both"),
            ],
            callback_data="visitor-visiting-to",
        )
    except Exception:
        logger.exception("visitor visiting-to buttons failed")
        deps.send_to(sender, "Visiting to? Reply UNIT I, UNIT II, or BOTH.")


def _parse_purpose_choice(incoming: str) -> str | None:
    key = (incoming or "").strip().lower().replace(" ", "_")
    mapping = {
        "customer_visit": PURPOSE_CUSTOMER,
        "visitor_coming_for_customer": PURPOSE_CUSTOMER,
        "visitor_purpose_customer": PURPOSE_CUSTOMER,
        "customer": PURPOSE_CUSTOMER,
        "other": PURPOSE_OTHER,
        "visitor_coming_for_other": PURPOSE_OTHER,
        "visitor_purpose_other": PURPOSE_OTHER,
    }
    if key in mapping:
        return mapping[key]
    um = (incoming or "").strip().upper()
    if um in (PURPOSE_CUSTOMER, PURPOSE_OTHER):
        return um
    return None


def _parse_visit_date(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(raw, fmt).date()
            return d.strftime("%d-%m-%Y")
        except ValueError:
            continue
    return None


def _parse_names(text: str) -> tuple[list[str] | None, str]:
    raw = (text or "").strip()
    if len(raw) < 2:
        return None, "Enter visitor name."
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    if not parts:
        return None, "Enter visitor name."
    if any(len(p) < 2 for p in parts):
        return None, "Each name must be at least 2 characters."
    return parts, ""


def _send_purpose_picker(sender: str, deps: VisitorDeps) -> None:
    rows = [
        {"id": "visitor_coming_for_customer", "title": "Customer Visit"},
        {"id": "visitor_coming_for_other", "title": "Other"},
        {"id": "back", "title": "Back"},
    ]
    try:
        send_list_menu(
            wa_id_to_phone(sender),
            "Purpose of visit?",
            rows,
            button_label="Select purpose",
            section_title="Purpose",
            callback_data="visitor-purpose",
        )
    except Exception:
        logger.exception("visitor purpose list failed")
        deps.send_to(sender, "Purpose of visit?")


def _prompt_whatsapp(sender: str, deps: VisitorDeps) -> None:
    deps.send_to(
        sender,
        "Visitor mobile number?\n10-digit mobile (e.g. 9876543210).",
    )


def _handle_coming_on(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.clear_session(sender)
        deps.go_main_menu(sender)
        return
    visit_date = _parse_visit_date(incoming)
    if not visit_date:
        deps.send_to(sender, "Coming On (DD-MM-YYYY)?")
        return
    deps.session_merge(sender, state=VISITOR_COMING_FROM, coming_on_date=visit_date)
    deps.send_to(sender, "Coming From?")


def _handle_coming_from(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.session_merge(sender, state=VISITOR_COMING_ON)
        deps.send_to(sender, "Coming On (DD-MM-YYYY)?")
        return
    detail = (incoming or "").strip()
    if len(detail) < 2:
        deps.send_to(sender, "Coming From?")
        return
    deps.session_merge(sender, state=VISITOR_PURPOSE, coming_from=detail)
    _send_purpose_picker(sender, deps)


def _handle_purpose(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.session_merge(sender, state=VISITOR_COMING_FROM)
        deps.send_to(sender, "Coming From?")
        return
    choice = _parse_purpose_choice(incoming)
    if not choice:
        deps.send_to(sender, "Select purpose from list.")
        _send_purpose_picker(sender, deps)
        return
    if choice == PURPOSE_OTHER:
        deps.session_merge(
            sender,
            state=VISITOR_PURPOSE_OTHER,
            purpose=PURPOSE_OTHER,
            purpose_label=PURPOSE_LABELS[PURPOSE_OTHER],
            purpose_detail="",
            coming_for=PURPOSE_OTHER,
            coming_for_label=PURPOSE_LABELS[PURPOSE_OTHER],
        )
        deps.send_to(sender, "Enter purpose of visit.")
        return
    deps.session_merge(
        sender,
        state=VISITOR_VISITING_TO,
        purpose=choice,
        purpose_label=PURPOSE_LABELS[choice],
        purpose_detail="",
        coming_for=choice,
        coming_for_label=PURPOSE_LABELS[choice],
    )
    _send_visiting_to_picker(sender, deps)


def _handle_purpose_other(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.session_merge(sender, state=VISITOR_PURPOSE)
        _send_purpose_picker(sender, deps)
        return
    detail = (incoming or "").strip()
    if len(detail) < 2:
        deps.send_to(sender, "Enter purpose of visit.")
        return
    deps.session_merge(
        sender,
        state=VISITOR_VISITING_TO,
        purpose=PURPOSE_OTHER,
        purpose_label=detail,
        purpose_detail=detail,
        coming_for=PURPOSE_OTHER,
        coming_for_label=detail,
    )
    _send_visiting_to_picker(sender, deps)


def _handle_visiting_to(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        if (session.get("purpose") or "").strip().upper() == PURPOSE_OTHER:
            deps.session_merge(sender, state=VISITOR_PURPOSE_OTHER)
            deps.send_to(sender, "Enter purpose of visit.")
        else:
            deps.session_merge(sender, state=VISITOR_PURPOSE)
            _send_purpose_picker(sender, deps)
        return
    choice = _parse_visiting_to_choice(incoming)
    if not choice:
        deps.send_to(sender, "Select Unit I, Unit II, or Both.")
        _send_visiting_to_picker(sender, deps)
        return
    deps.session_merge(
        sender,
        state=VISITOR_COUNT,
        visiting_to=choice,
        visiting_to_label=_visiting_to_label(choice),
    )
    deps.send_to(sender, "No of people?")


def _handle_count(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.session_merge(sender, state=VISITOR_VISITING_TO)
        _send_visiting_to_picker(sender, deps)
        return
    count = _parse_count_choice(incoming)
    if count is None:
        deps.send_to(sender, f"No of people? ({VISITOR_MIN_PEOPLE}-{VISITOR_MAX_PEOPLE})")
        return
    deps.session_merge(sender, state=VISITOR_NAMES, people_count=count)
    deps.send_to(sender, "Name of the Visitor?")


def _handle_names(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.session_merge(sender, state=VISITOR_COUNT)
        deps.send_to(sender, "No of people?")
        return
    names, err = _parse_names(incoming)
    if names is None:
        deps.send_to(sender, err)
        return
    deps.session_merge(sender, state=VISITOR_GUEST_PHONE, visitor_names=names)
    _prompt_whatsapp(sender, deps)


def _handle_guest_phone(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.session_merge(sender, state=VISITOR_NAMES, guest_phone="")
        deps.send_to(sender, "Name of the Visitor?")
        return

    d = digits(incoming)
    if len(d) < 10:
        deps.send_to(sender, "Enter a valid 10-digit number.")
        return

    phone10 = d[-10:]
    deps.session_merge(sender, state=VISITOR_CONFIRM, guest_phone=phone10)
    _show_confirm(sender, session, deps, guest_phone=phone10)


def _show_confirm(sender: str, session: dict, deps: VisitorDeps, **updates) -> None:
    data = {**session, **updates}
    count = _people_count(data)
    names = data.get("visitor_names") or []
    coming_on = (data.get("coming_on_date") or "").strip() or "—"
    coming_from = (data.get("coming_from") or "").strip() or "—"
    coming_for = (
        data.get("purpose_label")
        or data.get("coming_for_label")
        or ""
    ).strip() or "—"
    visiting = (
        data.get("visiting_to_label")
        or _visiting_to_label(data.get("visiting_to") or "")
    )
    guest = data.get("guest_phone") or ""

    body = (
        "Confirm:\n\n"
        f"Coming On: {coming_on}\n"
        f"Coming From: {coming_from}\n"
        f"Purpose: {coming_for}\n"
        f"Visiting to: {visiting}\n"
        f"People: {count}\n"
        f"Names: {', '.join(names)}\n"
        f"Visitor Mobile: {guest}\n"
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
    coming_on: str,
    coming_from: str,
    purpose: str,
    visiting_to: str,
    names: list,
    count: int,
    guest_phone: str,
) -> str:
    name_str = ", ".join(names) if names else "—"
    return (
        f"Coming On: {coming_on} | From: {coming_from} | Purpose: {purpose} | "
        f"Visiting to: {visiting_to} | People: {count} | Names: {name_str} | "
        f"Visitor Mobile: {guest_phone}"
    )


def _handle_confirm(sender: str, incoming: str, session: dict, deps: VisitorDeps) -> None:
    um = (incoming or "").strip().upper()
    if um == "BACK":
        deps.session_merge(sender, state=VISITOR_GUEST_PHONE)
        _prompt_whatsapp(sender, deps)
        return
    if um != "SUBMIT":
        deps.send_to(sender, "Reply SUBMIT to send the request, or CANCEL.")
        return
    _submit(sender, session, deps)


def _flow_pick(data: dict, *needles: str) -> str:
    """Read flow field by exact key or substring match (Interakt screen field ids)."""
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
    text = (raw or "").strip()
    if not text:
        return ""
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return text[:32]


def parse_flow_response(response_json: dict | str | None) -> dict | None:
    """Map WhatsApp Flow submit payload to visitor request fields."""
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

    coming_on = _normalize_flow_date(
        _flow_pick(data, "coming_on", "coming_on_date", "visit_date", "date")
    )
    coming_from = _flow_pick(data, "coming_from", "comingfrom")
    purpose_raw = _flow_pick(data, "purpose", "purpose_of_visit", "visit_purpose").lower()
    other_purpose = _flow_pick(
        data, "other_purpose", "enter_purpose", "purpose_other", "fill_purpose", "other"
    )

    if "customer" in purpose_raw or purpose_raw in ("customer_visit", "customer"):
        purpose = PURPOSE_CUSTOMER
        purpose_label = PURPOSE_LABELS[PURPOSE_CUSTOMER]
        purpose_detail = ""
    elif "other" in purpose_raw or other_purpose:
        purpose = PURPOSE_OTHER
        purpose_label = other_purpose or PURPOSE_LABELS[PURPOSE_OTHER]
        purpose_detail = other_purpose
    else:
        purpose = PURPOSE_CUSTOMER
        purpose_label = PURPOSE_LABELS[PURPOSE_CUSTOMER]
        purpose_detail = ""

    count_raw = _flow_pick(data, "no_of_people", "people_count", "number_of_people")
    if count_raw:
        try:
            count = int(digits(count_raw) or count_raw)
        except (TypeError, ValueError):
            count = VISITOR_MIN_PEOPLE
    else:
        count = VISITOR_MIN_PEOPLE  # form may omit "No of people" (defaults to 1)
    count = max(VISITOR_MIN_PEOPLE, min(VISITOR_MAX_PEOPLE, count))

    name_raw = _flow_pick(
        data, "visitor_name", "name_of_visitor", "visitor_names", "name"
    )
    names, _ = _parse_names(name_raw) if name_raw else (None, "Enter visitor name.")
    if not names:
        return None

    guest_phone = digits(
        _flow_pick(data, "visitor_mobile", "visitor_mobile_number", "mobile", "mobile_number")
    )
    if len(guest_phone) < 10:
        return None
    guest_phone = guest_phone[-10:]

    if not coming_on or not coming_from:
        return None

    return {
        "coming_on_date": coming_on,
        "coming_from": coming_from,
        "purpose": purpose,
        "purpose_label": purpose_label,
        "purpose_detail": purpose_detail,
        "coming_for": purpose,
        "coming_for_label": purpose_label,
        "people_count": count,
        "visitor_names": names,
        "guest_phone": guest_phone,
    }


def handle_flow_submission(sender: str, response_json: dict | str | None, deps: VisitorDeps) -> None:
    parsed = parse_flow_response(response_json)
    if not parsed:
        deps.send_to(sender, "Could not read the form. Please submit again or contact admin.")
        return
    _submit_payload(sender, parsed, deps)


def _submit(sender: str, session: dict, deps: VisitorDeps) -> None:
    payload = {
        "coming_on_date": (session.get("coming_on_date") or "").strip(),
        "coming_from": (session.get("coming_from") or "").strip(),
        "purpose": (session.get("purpose") or session.get("coming_for") or "").strip(),
        "purpose_label": (
            session.get("purpose_label")
            or session.get("coming_for_label")
            or PURPOSE_LABELS.get((session.get("purpose") or "").strip().upper(), "")
        ).strip(),
        "purpose_detail": (session.get("purpose_detail") or "").strip(),
        "coming_for": (session.get("coming_for") or session.get("purpose") or "").strip(),
        "coming_for_label": (
            session.get("coming_for_label") or session.get("purpose_label") or ""
        ).strip(),
        "people_count": _people_count(session),
        "visitor_names": list(session.get("visitor_names") or []),
        "guest_phone": (session.get("guest_phone") or "").strip(),
        "visiting_to": (session.get("visiting_to") or "").strip(),
        "visiting_to_label": (session.get("visiting_to_label") or "").strip(),
    }
    _submit_payload(sender, payload, deps)


def _submit_payload(sender: str, data: dict, deps: VisitorDeps) -> None:
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
    visiting_to = (data.get("visiting_to") or "").strip()
    chain = deps.build_approval_chain(ud, sender, visiting_to=visiting_to)
    if not chain:
        deps.clear_session(sender)
        deps.send_to(
            sender,
            visitor_chain_failure_message(
                ud,
                visiting_to=visiting_to,
                employee_wa=sender,
            ),
        )
        return
    if (visiting_to or "").strip().upper() == VISITING_BOTH and chain.get("mode") != "dual":
        deps.clear_session(sender)
        deps.send_to(
            sender,
            "Both units could not be routed to two JMDs.\nPlease contact admin.",
        )
        logger.error(
            "VISITOR BOTH misconfigured visiting_to=%s chain_mode=%s",
            visiting_to,
            chain.get("mode"),
        )
        return

    names = list(data.get("visitor_names") or [])
    count = int(data.get("people_count") or len(names) or 1)
    guest_phone = (data.get("guest_phone") or "").strip()
    coming_on = (data.get("coming_on_date") or "").strip()
    coming_from = (data.get("coming_from") or "").strip()
    purpose = (data.get("purpose") or data.get("coming_for") or "").strip()
    purpose_label = (data.get("purpose_label") or data.get("coming_for_label") or "").strip()
    purpose_detail = (data.get("purpose_detail") or "").strip()
    visiting_to_label = (
        data.get("visiting_to_label") or _visiting_to_label(visiting_to)
    ).strip()
    guest_wa = wa_from_10(guest_phone)
    summary = _build_summary(
        coming_on,
        coming_from,
        purpose_label,
        visiting_to_label,
        names,
        count,
        guest_phone,
    )

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
        "coming_on_date": coming_on,
        "visit_date": coming_on,
        "people_count": count,
        "visitor_names": names,
        "coming_from": coming_from,
        "coming_from_label": coming_from,
        "purpose": purpose,
        "purpose_label": purpose_label,
        "purpose_detail": purpose_detail,
        "coming_for": purpose,
        "coming_for_label": purpose_label,
        "visit_for": purpose,
        "visit_for_label": purpose_label,
        "organization": coming_from,
        "guest_phone": guest_phone,
        "guest_whatsapp": guest_wa,
        "submission_source": "chat",
        "visiting_to": visiting_to,
        "visiting_to_label": visiting_to_label,
        "employee_jmd_route": chain.get("employee_jmd_route")
        or (ud.get("jmd_route") or "JMD1").strip().upper(),
        "md": chain["md"],
        "manager_status": "N/A",
        "md_status": "AWAITING_JMD",
        "visitor_otp": "",
    }
    if chain.get("mode") == "dual":
        payload.update({
            "visitor_dual_jmd": True,
            "jmd_i": chain["jmd_i"],
            "jmd_ii": chain["jmd_ii"],
            "jmd": chain["jmd_i"],
            "jmd_route": chain.get("employee_jmd_route") or "JMD1",
            "jmd_i_status": "PENDING",
            "jmd_ii_status": "PENDING",
            "jmd_status": "PENDING",
        })
    else:
        payload.update({
            "visitor_dual_jmd": False,
            "jmd": chain["jmd"],
            "jmd_route": chain["jmd_route"],
            "jmd_status": "PENDING",
            "cross_unit": bool(chain.get("cross")),
        })
    if chain.get("approval_test"):
        payload["approval_test"] = True
    ref.set(payload)
    logger.info(
        "VISITOR created %s visiting_to=%s mode=%s",
        request_id,
        visiting_to,
        chain.get("mode"),
    )

    rd = ref.get().to_dict()
    jmd_ok = deps.notify_visitor_on_submit(rd, request_id, chain)

    deps.clear_session(sender)
    msg = "Visitor request is submitted."
    if chain.get("approval_test"):
        msg += " (pilot test JMD/MD — OD approvers unchanged)."
    if not jmd_ok:
        if chain.get("mode") == "dual":
            msg += (
                "\n\nUnit I and Unit II JMD must both be notified on WhatsApp. "
                "Ask each JMD to send Hi to this Alubee number once, then contact admin."
            )
        else:
            route = chain.get("jmd_route") or "JMD"
            msg += (
                f"\n\nJMD ({route}) could not be notified on WhatsApp. "
                "Ask them to send Hi to this Alubee number once, then contact admin."
            )
    deps.send_to(sender, msg)
