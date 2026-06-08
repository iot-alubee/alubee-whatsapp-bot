"""
Alubee WhatsApp bot (Interakt) — main entry: webhook, menu, routing.

Request flows live in separate modules:
  - od_request.py
  - visitor_request.py
  - leave_request.py
  - permission_request.py
  - approval.py (shared JMD → MD)
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

import firebase_admin
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from firebase_admin import credentials, firestore
from google.api_core.exceptions import ResourceExhausted

import approval
import approver_availability
import bot_shared
from bot_shared import wa_from_env
import leave_request
import od_request
import permission_request
import visitor_request
from interakt_api import (
    phone_to_wa_id,
    send_list_menu,
    send_reply_buttons,
    send_text,
    wa_id_to_phone,
)

_APP_DIR = Path(__file__).resolve().parent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _load_env() -> None:
    env_file = _APP_DIR / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=True)
        return
    example = _APP_DIR / ".env.example"
    if example.is_file():
        load_dotenv(example, override=True)
        logger.warning(
            "Interakt/.env not found — loaded .env.example. "
            "Copy .env.example to .env for your real API key."
        )
        return
    logger.warning("No Interakt/.env — set INTERAKT_API_KEY before sending messages.")


_load_env()

FIREBASE_PROJECT_ID = (os.getenv("FIREBASE_PROJECT_ID") or "whatsapp-approval-system").strip()


def _wa_from_mobile(mobile: str) -> str:
    digits = "".join(c for c in (mobile or "") if c.isdigit())
    if len(digits) == 10:
        return f"whatsapp:+91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"whatsapp:+{digits}"
    return ""


JMD_I_WHATSAPP_NUMBER = (
    os.getenv("JMD_I_WHATSAPP_NUMBER")
    or os.getenv("JMD_WHATSAPP_NUMBER")
    or _wa_from_mobile("7339221730")
).strip()
JMD_II_WHATSAPP_NUMBER = (
    os.getenv("JMD_II_WHATSAPP_NUMBER") or _wa_from_mobile("9659756070")
).strip()
MD_WHATSAPP_NUMBER = (
    os.getenv("MD_WHATSAPP_NUMBER") or _wa_from_mobile("7538866308")
).strip()
# Optional legacy test approver (old leave/permission rows only). Not used for new requests.
TEST_MD_WHATSAPP_NUMBER = wa_from_env("TEST_MD_WHATSAPP_NUMBER")
PPC_WHATSAPP_NUMBER = wa_from_env("PPC_WHATSAPP_NUMBER")
HR_WHATSAPP_NUMBER = wa_from_env("HR_WHATSAPP_NUMBER")

WHATSAPP_SESSION_HOURS = int(os.getenv("WHATSAPP_SESSION_HOURS", "24"))


def _parse_whatsapp_id_set(env_value: str) -> frozenset[str]:
    """Comma-separated whatsapp:+91… or 10-digit mobiles → normalized wa ids."""
    out: set[str] = set()
    for part in (env_value or "").split(","):
        raw = part.strip()
        if not raw:
            continue
        if raw.lower().startswith("whatsapp:"):
            out.add(raw.lower())
            continue
        digits = "".join(c for c in raw if c.isdigit())
        if len(digits) == 10:
            out.add(f"whatsapp:+91{digits}")
        elif len(digits) == 12 and digits.startswith("91"):
            out.add(f"whatsapp:+{digits}")
    return frozenset(out)


# Visitor uses same approvers as OD (JMD_I / JMD_II / MD above).
VISITOR_ROUTE_BY_UNIT = os.getenv("VISITOR_ROUTE_BY_UNIT", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Optional pilot: only these employees use test visitor approvers instead.
VISITOR_TEST_JMD_I_WHATSAPP_NUMBER = (
    os.getenv("VISITOR_TEST_JMD_I_WHATSAPP_NUMBER")
    or os.getenv("VISITOR_TEST_JMD_WHATSAPP_NUMBER")
    or ""
).strip()
VISITOR_TEST_JMD_II_WHATSAPP_NUMBER = (
    os.getenv("VISITOR_TEST_JMD_II_WHATSAPP_NUMBER")
    or os.getenv("VISITOR_TEST_JMD_WHATSAPP_NUMBER")
    or VISITOR_TEST_JMD_I_WHATSAPP_NUMBER
).strip()
VISITOR_TEST_MD_WHATSAPP_NUMBER = (os.getenv("VISITOR_TEST_MD_WHATSAPP_NUMBER") or "").strip()
VISITOR_TEST_EMPLOYEE_WA_IDS = _parse_whatsapp_id_set(
    os.getenv("VISITOR_TEST_EMPLOYEE_WHATSAPP_NUMBERS", "")
)

REQUEST_CANNOT_BE_RAISED_MSG = "This request cannot be raised now. Thanks!"
VISITOR_ALREADY_PENDING_MSG = "You already have a visitor request pending approval."

_UNSUPPORTED_REQUEST_IDS = frozenset({
    "VEHICLE_REQUEST",
})

SESSION_MENU_IDLE = "MENU_IDLE"
SESSION_AWAITING_HI = "AWAITING_HI"
SESSION_APPROVER_AVAILABILITY = "APPROVER_AVAILABILITY"

_ROW_IDS = {
    "od_request": "OD_REQUEST",
    "od_form": "OD_FORM",
    "od_-_form": "OD_FORM",
    "vehicle_request": "VEHICLE_REQUEST",
    "leave_request": "LEAVE_REQUEST",
    "permission_request": "PERMISSION_REQUEST",
    "visitor_request": "VISITOR_REQUEST",
    "unit_i": "UNIT_I",
    "unit_ii": "UNIT_II",
    "unit_1": "UNIT_I",
    "unit_2": "UNIT_II",
    "other": "OTHER",
    "yes": "YES",
    "no": "NO",
    "back": "BACK",
    "submit": "SUBMIT",
    "cancel": "CANCEL",
    "approve": "APPROVE",
    "deny": "DENY",
    "online": "ONLINE",
    "offline": "OFFLINE",
    "visitor_coming_for_customer": "CUSTOMER_VISIT",
    "visitor_coming_for_technical": "TECHNICAL_WORK",
    "visitor_coming_for_other": "OTHER",
    "visitor_visit_unit_i": "UNIT_I",
    "visitor_visit_unit_ii": "UNIT_II",
    "visitor_visit_both": "BOTH",
    "customer_visit": "CUSTOMER_VISIT",
    "technical_work": "TECHNICAL_WORK",
    "leave_today": "LEAVE_TODAY",
    "leave_tomorrow": "LEAVE_TOMORROW",
    "leave_other": "LEAVE_OTHER",
    "sick_leave": "SICK_LEAVE",
    "casual_leave": "CASUAL_LEAVE",
    "leave_reason_other": "LEAVE_REASON_OTHER",
    "leave_cancel": "LEAVE_CANCEL",
    "cancel_leave": "LEAVE_CANCEL",
    "leave_exit": "LEAVE_EXIT",
    "permission_cancel": "PERMISSION_CANCEL",
    "cancel_permission": "PERMISSION_CANCEL",
    "permission_exit": "PERMISSION_EXIT",
    "permission_late_in": "PERMISSION_LATE_IN",
    "late_in": "PERMISSION_LATE_IN",
    "permission_early_out": "PERMISSION_EARLY_OUT",
    "early_out": "PERMISSION_EARLY_OUT",
    "permission_other": "PERMISSION_OTHER",
    "permission_for_myself": "PERMISSION_FOR_MYSELF",
    "for_myself": "PERMISSION_FOR_MYSELF",
    "permission_for_cl": "PERMISSION_FOR_CL",
    "for_cl": "PERMISSION_FOR_CL",
    "permission_shift_i": "PERMISSION_SHIFT_I",
    "shift_i": "PERMISSION_SHIFT_I",
    "permission_shift_ii": "PERMISSION_SHIFT_II",
    "shift_ii": "PERMISSION_SHIFT_II",
}


def _init_firebase() -> None:
    opts = {"projectId": FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None
    try:
        firebase_admin.get_app()
        return
    except ValueError:
        pass
    cred_path = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if not cred_path:
        cred_path = str(_APP_DIR / "firebase-adminsdk.json")
    elif not os.path.isabs(cred_path):
        cred_path = str(_APP_DIR / cred_path)
    if not os.path.isfile(cred_path):
        cred_path = str(_APP_DIR.parent / "firebase-adminsdk.json")
    if os.path.isfile(cred_path):
        firebase_admin.initialize_app(credentials.Certificate(cred_path), opts)
        return
    firebase_admin.initialize_app(options=opts)


_init_firebase()
db = firestore.client()

app = FastAPI(title="Alubee Interakt bot")


@app.on_event("startup")
def _warmup_services() -> None:
    """Warm Firestore + TLS before first webhook (reduces cold-start SSL failures)."""
    try:
        list(db.collection("users").limit(1).stream())
        logger.info("Firestore warmup ok")
    except Exception:
        logger.exception("Firestore warmup failed (will retry on first message)")


def _is_transient_error(exc: BaseException) -> bool:
    from requests.exceptions import ConnectionError as RequestsConnectionError
    from requests.exceptions import SSLError as RequestsSSLError
    from requests.exceptions import Timeout as RequestsTimeout

    if isinstance(
        exc,
        (RequestsSSLError, RequestsConnectionError, RequestsTimeout, ssl.SSLEOFError),
    ):
        return True
    cause = getattr(exc, "__cause__", None)
    return isinstance(cause, ssl.SSLEOFError) if cause else False


def _utcnow():
    return datetime.now(timezone.utc)


def _session_ref(sender: str):
    return db.collection("sessions").document(sender)


def _whatsapp_activity_ref(wa_id: str):
    return db.collection("whatsapp_activity").document(wa_id)


def _touch_whatsapp_inbound(wa_id: str) -> None:
    _whatsapp_activity_ref(wa_id).set({"last_inbound_at": _utcnow()}, merge=True)


def _has_active_whatsapp_session(wa_id: str) -> bool:
    snap = _whatsapp_activity_ref(wa_id).get()
    if not snap.exists:
        return False
    last = snap.to_dict().get("last_inbound_at")
    if not last:
        return False
    if hasattr(last, "timestamp"):
        last_dt = datetime.fromtimestamp(last.timestamp(), tz=timezone.utc)
    elif isinstance(last, datetime):
        last_dt = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
    else:
        return False
    age_hours = (_utcnow() - last_dt).total_seconds() / 3600
    return age_hours < WHATSAPP_SESSION_HOURS


def _session_merge(sender: str, **fields) -> None:
    _session_ref(sender).set(fields, merge=True)


def _chat_name(name) -> str:
    raw = str(name or "").strip()
    return raw.title() if raw else "Employee"


def _same_whatsapp(a: str, b: str) -> bool:
    return bool(a and b and a.strip().lower() == b.strip().lower())


def _send_to(wa_id: str, text: str) -> None:
    try:
        send_text(wa_id_to_phone(wa_id), text)
    except Exception:
        logger.exception("send_text failed to=%s", wa_id)


bot_shared.configure(
    db=db,
    send_to=_send_to,
    session_ref=_session_ref,
    session_merge=_session_merge,
    utcnow=_utcnow,
    has_active_whatsapp_session=_has_active_whatsapp_session,
    chat_name=_chat_name,
    same_whatsapp=_same_whatsapp,
)


def _on_visitor_md_approved(ref, rd: dict) -> None:
    visitor_request.send_otps_after_md_approve(ref, rd, _send_to)


approval.configure(
    approval.ApprovalDeps(
        db=db,
        send_to=_send_to,
        session_merge=_session_merge,
        session_ref=_session_ref,
        utcnow=_utcnow,
        chat_name=_chat_name,
        same_whatsapp=_same_whatsapp,
        has_active_whatsapp_session=_has_active_whatsapp_session,
        jmd_i=JMD_I_WHATSAPP_NUMBER,
        jmd_ii=JMD_II_WHATSAPP_NUMBER,
        md=MD_WHATSAPP_NUMBER,
        test_md=TEST_MD_WHATSAPP_NUMBER,
        whatsapp_session_hours=WHATSAPP_SESSION_HOURS,
        menu_idle_state=SESSION_MENU_IDLE,
        on_visitor_md_approved=_on_visitor_md_approved,
        visitor_jmd_i=JMD_I_WHATSAPP_NUMBER,
        visitor_jmd_ii=JMD_II_WHATSAPP_NUMBER,
        visitor_md=MD_WHATSAPP_NUMBER,
        visitor_route_by_unit=VISITOR_ROUTE_BY_UNIT,
        visitor_test_jmd_i=VISITOR_TEST_JMD_I_WHATSAPP_NUMBER,
        visitor_test_jmd_ii=VISITOR_TEST_JMD_II_WHATSAPP_NUMBER,
        visitor_test_md=VISITOR_TEST_MD_WHATSAPP_NUMBER,
        visitor_test_employee_wa_ids=VISITOR_TEST_EMPLOYEE_WA_IDS,
        ppc=PPC_WHATSAPP_NUMBER,
        hr=HR_WHATSAPP_NUMBER,
    )
)
logger.info(
    "visitor approvers (same as OD) jmd_i=%s jmd_ii=%s md=%s both_units_ok=%s",
    JMD_I_WHATSAPP_NUMBER or "(missing)",
    JMD_II_WHATSAPP_NUMBER or "(missing)",
    MD_WHATSAPP_NUMBER or "(missing)",
    bool(
        JMD_I_WHATSAPP_NUMBER
        and JMD_II_WHATSAPP_NUMBER
        and not _same_whatsapp(JMD_I_WHATSAPP_NUMBER, JMD_II_WHATSAPP_NUMBER)
    ),
)


def _build_visitor_approval_chain(
    user_data: dict, employee_wa: str, visiting_to: str = ""
) -> dict | None:
    return approval.build_approval_chain(
        user_data,
        request_type="VISITOR",
        employee_wa=employee_wa,
        visiting_to=visiting_to,
    )


def _build_permission_approval_chain(
    user_data: dict | None = None,
    *,
    permission_for: str = "myself",
) -> dict | None:
    return approval.build_permission_approval_chain(
        user_data,
        permission_for=permission_for,
    )


def _go_main_menu_for_employee(sender: str) -> None:
    exists, ud = bot_shared.get_user_record(sender)
    if exists and ud:
        name = ud.get("name", "Employee")
        _session_merge(sender, state=SESSION_MENU_IDLE, employee_name=name)
        _send_main_menu(sender, name)
    else:
        _session_ref(sender).delete()
        _send_to(sender, "User not registered.\nPlease contact admin.")


OD_DEPS = od_request.OdDeps(
    db=db,
    send_to=_send_to,
    session_merge=_session_merge,
    session_ref=_session_ref,
    utcnow=_utcnow,
    chat_name=_chat_name,
    same_whatsapp=_same_whatsapp,
    build_approval_chain=approval.build_approval_chain,
    notify_jmd=approval.notify_jmd,
    go_main_menu=_go_main_menu_for_employee,
    awaiting_hi_state=SESSION_AWAITING_HI,
    already_pending_msg=od_request.OD_ALREADY_PENDING_MSG,
)

LEAVE_DEPS = leave_request.LeaveDeps(
    db=db,
    send_to=_send_to,
    session_merge=_session_merge,
    session_ref=_session_ref,
    utcnow=_utcnow,
    chat_name=_chat_name,
    build_approval_chain=approval.build_leave_approval_chain,
    notify_jmd=approval.notify_jmd,
    go_main_menu=_go_main_menu_for_employee,
)

PERMISSION_DEPS = permission_request.PermissionDeps(
    db=db,
    send_to=_send_to,
    session_merge=_session_merge,
    session_ref=_session_ref,
    utcnow=_utcnow,
    chat_name=_chat_name,
    build_approval_chain=_build_permission_approval_chain,
    notify_jmd=approval.notify_jmd,
    go_main_menu=_go_main_menu_for_employee,
)

VISITOR_DEPS = visitor_request.VisitorDeps(
    db=db,
    send_to=_send_to,
    session_merge=_session_merge,
    session_ref=_session_ref,
    utcnow=_utcnow,
    build_approval_chain=_build_visitor_approval_chain,
    notify_visitor_on_submit=approval.notify_visitor_on_submit,
    clear_session=lambda sender: _session_ref(sender).delete(),
    go_main_menu=_go_main_menu_for_employee,
    already_pending_msg=VISITOR_ALREADY_PENDING_MSG,
)


def _numbered_request_menu(employee_name: str) -> str:
    name = _chat_name(employee_name)
    return (
        f"Welcome {name} 👋\n\n"
        "Select an option (reply with the number):\n"
        "1. OD Request\n"
        "2. Vehicle Request\n"
        "3. Leave Request\n"
        "4. Permission Request\n"
        "5. Visitor Request\n"
        "6. OD - Form"
    )


def _list_rows(*items: tuple[str, str]) -> list[dict[str, str]]:
    return [{"id": row_id, "title": title[:24]} for row_id, title in items]


def _send_approver_availability_menu(wa_id: str, current: str) -> None:
    label = "Offline" if current == "offline" else "Online"
    try:
        send_reply_buttons(
            wa_id_to_phone(wa_id),
            f"Approver status: {label}\n\nSet your availability:",
            [("ONLINE", "Online"), ("OFFLINE", "Offline")],
        )
    except Exception:
        logger.exception("approver availability menu failed to=%s", wa_id)
        _send_to(
            wa_id,
            f"Status: {label}\nReply ONLINE or OFFLINE to change.",
        )


def _try_handle_approver_availability(sender: str, incoming: str) -> bool:
    """Online/Offline for JMD/MD; returns True if handled."""
    upper = (incoming or "").strip().upper()
    if upper not in ("ONLINE", "OFFLINE"):
        return False
    snap = _session_ref(sender).get()
    data = snap.to_dict() if snap.exists else {}
    role = (data.get("approver_role") or "").strip()
    if data.get("state") != SESSION_APPROVER_AVAILABILITY and not role:
        role = approver_availability.approver_role_for_sender(
            sender,
            md=MD_WHATSAPP_NUMBER,
            jmd_i=JMD_I_WHATSAPP_NUMBER,
            jmd_ii=JMD_II_WHATSAPP_NUMBER,
            same_whatsapp=_same_whatsapp,
            test_md=TEST_MD_WHATSAPP_NUMBER,
        )
        if not role:
            return False
    availability = "offline" if upper == "OFFLINE" else "online"
    if availability == "offline":
        blocked = approver_availability.offline_blocked_message(
            db,
            role,
            md=MD_WHATSAPP_NUMBER,
            jmd_i=JMD_I_WHATSAPP_NUMBER,
            jmd_ii=JMD_II_WHATSAPP_NUMBER,
        )
        if blocked:
            _send_to(sender, blocked)
            return True
    approver_availability.set_availability(
        db, sender, availability, role=role or "approver"
    )
    _send_to(sender, f"You are now {availability.title()}.")
    exists, ud = bot_shared.get_user_record(sender)
    if exists and ud:
        name = ud.get("name", "Approver")
        _session_merge(sender, state=SESSION_MENU_IDLE, employee_name=name)
        _send_main_menu(sender, name)
    else:
        _session_merge(sender, state=SESSION_APPROVER_AVAILABILITY, approver_role=role)
    return True


def _send_main_menu(wa_id: str, employee_name: str) -> None:
    name = _chat_name(employee_name)
    welcome = f"Welcome {name} 👋\n\nPlease choose an option:"
    rows = _list_rows(
        ("od_request", "OD Request"),
        ("vehicle_request", "Vehicle Request"),
        ("leave_request", "Leave Request"),
        ("permission_request", "Permission Request"),
        ("visitor_request", "Visitor Request"),
        ("od_form", "OD - Form"),
    )
    try:
        send_list_menu(
            wa_id_to_phone(wa_id),
            welcome,
            rows,
            callback_data="main-menu",
        )
    except Exception:
        logger.exception("main menu InteractiveList failed to=%s", wa_id)
        try:
            _send_to(wa_id, _numbered_request_menu(employee_name))
        except Exception:
            logger.exception("numbered menu text failed to=%s", wa_id)
            _send_to(wa_id, f"{welcome}\n\nReply with 1 for OD, 3 for Leave, 5 for Visitor.")


def _normalize_choice(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    key = s.lower().replace(" ", "_").replace("-", "_")
    if key in _ROW_IDS:
        return _ROW_IDS[key]
    if key.upper() in _ROW_IDS.values():
        return key.upper()
    titles = {
        "od request": "OD_REQUEST",
        "od - form": "OD_FORM",
        "od form": "OD_FORM",
        "vehicle request": "VEHICLE_REQUEST",
        "leave request": "LEAVE_REQUEST",
        "permission request": "PERMISSION_REQUEST",
        "visitor request": "VISITOR_REQUEST",
    }
    return titles.get(s.lower(), s)


def _coerce_flow_response(response) -> dict | str | None:
    if response is None:
        return None
    if isinstance(response, dict):
        return response if response else None
    if isinstance(response, str):
        text = response.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else text
        except json.JSONDecodeError:
            return text
    return None


def _deep_find_flow_response(obj, depth: int = 0) -> dict | str | None:
    """Walk Interakt webhook JSON for flow submit payload (shape varies by event)."""
    if depth > 10:
        return None
    if isinstance(obj, dict):
        if obj.get("type") == "nfm_reply":
            nfm = obj.get("nfm_reply") or {}
            found = _coerce_flow_response(
                nfm.get("response_json") or nfm.get("response") or nfm.get("body")
            )
            if found:
                return found
        for key in ("response_json", "response", "flow_response", "body"):
            if key in obj:
                found = _coerce_flow_response(obj[key])
                if found:
                    return found
        for value in obj.values():
            found = _deep_find_flow_response(value, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find_flow_response(item, depth + 1)
            if found:
                return found
    return None


def _flow_callback_kind(body: dict) -> str:
    data = body.get("data") or {}
    callback = (
        (body.get("callbackData") or data.get("callbackData") or "")
        .strip()
        .lower()
    )
    if callback in ("od-flow", "od_form", "od"):
        return "od-flow"
    if callback in ("visitor-flow", "visitor_form", "visitor"):
        return "visitor-flow"
    return callback


def _is_flow_reply_webhook(body: dict) -> bool:
    wtype = (body.get("type") or "").strip().lower()
    if wtype == "message_api_flow_response":
        return True
    if _flow_callback_kind(body) in ("od-flow", "visitor-flow"):
        return True
    data = body.get("data") or {}
    msg_obj = data.get("message") or {}
    raw_msg = msg_obj.get("message")
    if isinstance(raw_msg, dict) and raw_msg.get("type") == "nfm_reply":
        return True
    content_type = (msg_obj.get("message_content_type") or "").strip().lower()
    if content_type in ("interactiveflowreply", "flow", "nfm_reply"):
        return True
    if _deep_find_flow_response(body):
        return True
    return False


def _extract_message(message_field) -> str:
    if isinstance(message_field, dict):
        if message_field.get("type") == "nfm_reply":
            return ""
        if message_field.get("type") == "list_reply":
            lr = message_field.get("list_reply") or {}
            if lr.get("id"):
                return str(lr["id"])
        if message_field.get("type") == "button_reply":
            br = message_field.get("button_reply") or {}
            if br.get("id"):
                return str(br["id"])
        br = message_field.get("button_reply")
        if isinstance(br, dict) and br.get("id"):
            return str(br["id"])
        lr = message_field.get("list_reply")
        if isinstance(lr, dict) and lr.get("id"):
            return str(lr["id"])
        return str(
            message_field.get("id")
            or message_field.get("title")
            or message_field.get("message")
            or message_field.get("text")
            or ""
        )
    raw = str(message_field or "").strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return _extract_message(parsed)
        except json.JSONDecodeError:
            pass
    return raw


def _flow_response_from_message(msg_obj: dict) -> dict | str | None:
    """Extract nfm_reply.response_json from Interakt flow submit webhooks."""
    raw_msg = msg_obj.get("message")
    if not isinstance(raw_msg, dict):
        return None
    if raw_msg.get("type") != "nfm_reply":
        return None
    nfm = raw_msg.get("nfm_reply") or {}
    return _coerce_flow_response(
        nfm.get("response_json") or nfm.get("response") or nfm.get("payload")
    )


def _parse_flow_webhook(body: dict) -> tuple[str, dict | str, str] | None:
    """Return (wa_id, response_json, flow_kind) for completed WhatsApp Flow submits."""
    wtype = (body.get("type") or "").strip().lower()
    if wtype not in ("message_received", "message_api_flow_response"):
        return None

    data = body.get("data") or {}
    customer = data.get("customer") or {}
    msg_obj = data.get("message") or {}

    phone = str(customer.get("phone_number") or "")
    if not phone:
        phone = str(customer.get("channel_phone_number") or "")
    if not phone:
        return None

    response = _flow_response_from_message(msg_obj)
    if response is None:
        response = _coerce_flow_response(data.get("response_json"))
    if response is None:
        response = _coerce_flow_response(msg_obj.get("response_json"))
    if response is None and wtype == "message_api_flow_response":
        response = _coerce_flow_response(
            data.get("flow_response") or msg_obj.get("flow_response")
        )
    if response is None:
        response = _deep_find_flow_response(body)
    if response is None:
        return None

    callback = _flow_callback_kind(body)
    if not callback and isinstance(response, dict):
        keys = {str(k).lower() for k in response.keys()}
        if keys & {"od_reason", "company_vehicle", "vehicle"}:
            callback = "od-flow"
        elif keys & {"coming_on", "coming_from", "purpose", "visitor_name"}:
            callback = "visitor-flow"
    return phone_to_wa_id(phone), response, callback or "flow"


def _parse_webhook(body: dict) -> tuple[str, str] | None:
    wtype = (body.get("type") or "").strip()
    if wtype != "message_received":
        return None

    data = body.get("data") or {}
    customer = data.get("customer") or {}
    msg_obj = data.get("message") or {}

    phone = str(customer.get("phone_number") or "")
    if not phone:
        phone = str(customer.get("channel_phone_number") or "")

    wa_id = phone_to_wa_id(phone)
    raw_msg = msg_obj.get("message")
    if isinstance(raw_msg, dict) and raw_msg.get("type") == "nfm_reply":
        return None
    incoming = _normalize_choice(_extract_message(raw_msg))
    logger.info(
        "parsed incoming=%s content_type=%s",
        incoming,
        msg_obj.get("message_content_type"),
    )
    return wa_id, incoming


def _process(sender: str, incoming: str) -> None:
    logger.info("process sender=%s incoming=%s", sender, incoming)

    if incoming.lower() in ("hi", "hello"):
        exists, ud = bot_shared.get_user_record(sender)
        try:
            _touch_whatsapp_inbound(sender)
        except Exception:
            logger.exception("whatsapp activity touch failed sender=%s", sender)
        approver_role = approver_availability.approver_role_for_sender(
            sender,
            md=MD_WHATSAPP_NUMBER,
            jmd_i=JMD_I_WHATSAPP_NUMBER,
            jmd_ii=JMD_II_WHATSAPP_NUMBER,
            same_whatsapp=_same_whatsapp,
            test_md=TEST_MD_WHATSAPP_NUMBER,
        )
        if approver_role:
            approver_availability.ensure_online_default(
                db, sender, role=approver_role
            )
            current = approver_availability.get_availability(db, sender)
            _session_merge(
                sender,
                state=SESSION_APPROVER_AVAILABILITY,
                approver_role=approver_role,
            )
            _send_approver_availability_menu(sender, current)
            return
        if exists and ud:
            name = ud.get("name", "Employee")
            _session_merge(sender, state=SESSION_MENU_IDLE, employee_name=name)
            _send_main_menu(sender, name)
        else:
            _session_ref(sender).delete()
            _send_to(sender, "User not registered.\nPlease contact admin.")
        return

    _touch_whatsapp_inbound(sender)

    if _try_handle_approver_availability(sender, incoming):
        return

    if approval.handle_approval_gate(sender, incoming):
        return

    session_doc = _session_ref(sender).get()
    session = session_doc.to_dict() if session_doc.exists else None
    state = (session or {}).get("state")

    if state == SESSION_AWAITING_HI:
        _send_to(sender, "Send Hi to start.")
        return

    if od_request.is_od_state(state):
        od_request.handle(sender, incoming, session or {}, OD_DEPS)
        return

    if visitor_request.is_visitor_state(state):
        visitor_request.handle(sender, incoming, session or {}, VISITOR_DEPS)
        return

    if leave_request.is_leave_state(state):
        leave_request.handle(sender, incoming, session or {}, LEAVE_DEPS)
        return

    if permission_request.is_permission_state(state):
        permission_request.handle(sender, incoming, session or {}, PERMISSION_DEPS)
        return

    if incoming == "1" or incoming == "OD_REQUEST":
        if state == SESSION_MENU_IDLE:
            od_request.try_start(sender, OD_DEPS)
        else:
            _send_to(sender, "Send Hi to start.")
        return

    if incoming in ("6", "OD_FORM"):
        if state == SESSION_MENU_IDLE:
            od_request.try_start_form(sender, OD_DEPS)
        else:
            _send_to(sender, "Send Hi to start.")
        return

    if incoming == "3" or incoming == "LEAVE_REQUEST":
        if state == SESSION_MENU_IDLE:
            leave_request.try_start(sender, LEAVE_DEPS)
        else:
            _send_to(sender, "Send Hi to start.")
        return

    if incoming == "4" or incoming == "PERMISSION_REQUEST":
        if state == SESSION_MENU_IDLE:
            permission_request.try_start(sender, PERMISSION_DEPS)
        else:
            _send_to(sender, "Send Hi to start.")
        return

    if incoming == "5" or incoming == "VISITOR_REQUEST":
        if state == SESSION_MENU_IDLE:
            visitor_request.try_start(sender, VISITOR_DEPS)
        else:
            _send_to(sender, "Send Hi to start.")
        return

    if incoming == "2" or incoming in _UNSUPPORTED_REQUEST_IDS:
        _send_to(sender, REQUEST_CANNOT_BE_RAISED_MSG)
        return

    if not incoming.strip():
        return

    if session_doc.exists:
        _send_to(sender, "Invalid session state")
        return

    _send_to(sender, "Send Hi to start.")


@app.get("/health")
def health():
    key = (os.getenv("INTERAKT_API_KEY") or "").strip()
    return {
        "status": "ok",
        "provider": "interakt",
        "api_key_set": bool(key),
        "env_file": "Interakt/.env" if (_APP_DIR / ".env").is_file() else "missing",
        "whatsapp_session_hours": WHATSAPP_SESSION_HOURS,
        "jmd_i": JMD_I_WHATSAPP_NUMBER,
        "jmd_ii": JMD_II_WHATSAPP_NUMBER,
        "md": MD_WHATSAPP_NUMBER,
        "test_md_configured": bool(TEST_MD_WHATSAPP_NUMBER),
        "visitor_uses_od_approvers": True,
        "visitor_approvers_configured": bool(
            JMD_I_WHATSAPP_NUMBER and MD_WHATSAPP_NUMBER
        ),
        "visitor_test_approvers_configured": bool(
            VISITOR_TEST_JMD_I_WHATSAPP_NUMBER
            and VISITOR_TEST_MD_WHATSAPP_NUMBER
            and VISITOR_TEST_EMPLOYEE_WA_IDS
        ),
        "visitor_test_employee_count": len(VISITOR_TEST_EMPLOYEE_WA_IDS),
        "visitor_otp_template": (
            (os.getenv("VISITOR_OTP_TEMPLATE_NAME") or "visitor_pass_code").strip()
        ),
        "od_form_configured": od_request.od_form_configured(),
        "od_flow_template": od_request.od_flow_template_name(),
    }


@app.post("/webhook")
@app.post("/")
async def webhook(request: Request):
    body = await request.json()
    logger.info("webhook: %s", json.dumps(body, default=str)[:2500])

    flow_parsed = _parse_flow_webhook(body)
    if flow_parsed:
        sender, response_json, flow_kind = flow_parsed
        if flow_kind in ("od-flow", "od_form", "od", "flow"):
            try:
                od_request.handle_flow_submission(sender, response_json, OD_DEPS)
            except Exception:
                logger.exception("OD flow submit failed sender=%s", sender)
                try:
                    _send_to(
                        sender,
                        "Sorry, we could not save your OD form. Please send Hi and try again.",
                    )
                except Exception:
                    logger.exception("could not notify user after OD flow error")
        else:
            logger.info("ignored flow submit kind=%s sender=%s", flow_kind, sender)
        return {"status": "success"}

    if _is_flow_reply_webhook(body):
        logger.warning(
            "flow reply webhook not parsed; skipping chat handler keys=%s",
            list((body.get("data") or {}).keys()),
        )
        return {"status": "success"}

    parsed = _parse_webhook(body)
    if parsed:
        sender, incoming = parsed
        try:
            for attempt in range(2):
                try:
                    _process(sender, incoming)
                    break
                except Exception as e:
                    if attempt == 0 and _is_transient_error(e):
                        logger.warning(
                            "transient error sender=%s retrying: %s", sender, e
                        )
                        time.sleep(1.5)
                        continue
                    raise
        except ResourceExhausted:
            logger.error("Firestore quota exceeded sender=%s", sender)
            try:
                _send_to(
                    sender,
                    "Service is busy (database limit). "
                    "Please wait 1-2 minutes and send Hi again.",
                )
            except Exception:
                logger.exception("could not notify user after quota error")
        except Exception as e:
            logger.exception("process failed sender=%s incoming=%s", sender, incoming)
            try:
                _send_to(
                    sender,
                    "Sorry, the bot had a temporary error. Please send Hi again.",
                )
            except Exception:
                logger.exception(
                    "could not notify user after error (%s)", type(e).__name__
                )

    return {"status": "success"}
