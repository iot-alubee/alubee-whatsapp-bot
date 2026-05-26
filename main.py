"""
Alubee WhatsApp bot (Interakt) — Cloud Run entry: webhook, menu, routing.

Request flows: od_request.py, visitor_request.py, approval.py
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import firebase_admin
from fastapi import FastAPI, Request
from firebase_admin import credentials, firestore

import approval
import bot_shared
import od_request
import visitor_request
from interakt_api import phone_to_wa_id, send_list_menu, send_text, wa_id_to_phone

_APP_DIR = Path(__file__).resolve().parent

_CLOUD_RUN_STRIP_CRED_ENV = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "FIREBASE_CREDENTIALS_PATH",
    "FIREBASE_CREDENTIALS_JSON",
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _running_on_cloud_run() -> bool:
    return bool(os.environ.get("K_SERVICE"))


if not _running_on_cloud_run():
    from dotenv import load_dotenv

    env_file = _APP_DIR / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=True)
    else:
        example = _APP_DIR / ".env.example"
        if example.is_file():
            load_dotenv(example, override=True)


@contextmanager
def _cloud_run_metadata_credentials_env():
    saved = {k: os.environ.pop(k) for k in _CLOUD_RUN_STRIP_CRED_ENV if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


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

WHATSAPP_SESSION_HOURS = int(os.getenv("WHATSAPP_SESSION_HOURS", "24"))


def _parse_whatsapp_id_set(env_value: str) -> frozenset[str]:
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


VISITOR_JMD_I_WHATSAPP_NUMBER = (
    os.getenv("VISITOR_JMD_I_WHATSAPP_NUMBER")
    or os.getenv("VISITOR_JMD_WHATSAPP_NUMBER")
    or ""
).strip()
VISITOR_JMD_II_WHATSAPP_NUMBER = (
    os.getenv("VISITOR_JMD_II_WHATSAPP_NUMBER")
    or os.getenv("VISITOR_JMD_WHATSAPP_NUMBER")
    or VISITOR_JMD_I_WHATSAPP_NUMBER
).strip()
VISITOR_MD_WHATSAPP_NUMBER = (os.getenv("VISITOR_MD_WHATSAPP_NUMBER") or "").strip()

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
    "LEAVE_REQUEST",
    "PERMISSION_REQUEST",
})

SESSION_MENU_IDLE = "MENU_IDLE"
SESSION_AWAITING_HI = "AWAITING_HI"

_ROW_IDS = {
    "od_request": "OD_REQUEST",
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
}


def _init_firebase() -> None:
    opts = {"projectId": FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None

    def _try_adc():
        cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred, opts)

    try:
        firebase_admin.get_app()
        return
    except ValueError:
        pass

    if _running_on_cloud_run():
        if any(os.environ.get(k) for k in _CLOUD_RUN_STRIP_CRED_ENV):
            logger.warning(
                "Cloud Run: use service account IAM for Firestore; "
                "ignoring GOOGLE_APPLICATION_CREDENTIALS / FIREBASE_CREDENTIALS_JSON."
            )
        with _cloud_run_metadata_credentials_env():
            _try_adc()
        return

    json_raw = (os.getenv("FIREBASE_CREDENTIALS_JSON") or "").strip()
    if json_raw:
        firebase_admin.initialize_app(credentials.Certificate(json.loads(json_raw)), opts)
        return

    cred_path = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if not cred_path:
        cred_path = str(_APP_DIR / "firebase-adminsdk.json")
    elif not os.path.isabs(cred_path):
        cred_path = str(_APP_DIR / cred_path)
    if not cred_path or not os.path.isfile(cred_path):
        cred_path = str(_APP_DIR.parent / "firebase-adminsdk.json")
    if os.path.isfile(cred_path):
        firebase_admin.initialize_app(credentials.Certificate(cred_path), opts)
        return

    _try_adc()


_init_firebase()
db = firestore.client()

if _running_on_cloud_run() and not (os.getenv("INTERAKT_API_KEY") or "").strip():
    raise RuntimeError("Cloud Run requires INTERAKT_API_KEY environment variable.")

app = FastAPI(title="Alubee Interakt bot")


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
        whatsapp_session_hours=WHATSAPP_SESSION_HOURS,
        menu_idle_state=SESSION_MENU_IDLE,
        on_visitor_md_approved=_on_visitor_md_approved,
        visitor_jmd_i=VISITOR_JMD_I_WHATSAPP_NUMBER,
        visitor_jmd_ii=VISITOR_JMD_II_WHATSAPP_NUMBER,
        visitor_md=VISITOR_MD_WHATSAPP_NUMBER,
        visitor_test_jmd_i=VISITOR_TEST_JMD_I_WHATSAPP_NUMBER,
        visitor_test_jmd_ii=VISITOR_TEST_JMD_II_WHATSAPP_NUMBER,
        visitor_test_md=VISITOR_TEST_MD_WHATSAPP_NUMBER,
        visitor_test_employee_wa_ids=VISITOR_TEST_EMPLOYEE_WA_IDS,
    )
)


def _build_visitor_approval_chain(user_data: dict, employee_wa: str) -> dict | None:
    return approval.build_approval_chain(
        user_data,
        request_type="VISITOR",
        employee_wa=employee_wa,
    )


def _go_main_menu_for_employee(sender: str) -> None:
    user = db.collection("users").document(sender).get()
    if user.exists:
        name = user.to_dict().get("name", "Employee")
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

VISITOR_DEPS = visitor_request.VisitorDeps(
    db=db,
    send_to=_send_to,
    session_merge=_session_merge,
    session_ref=_session_ref,
    utcnow=_utcnow,
    build_approval_chain=_build_visitor_approval_chain,
    notify_jmd=approval.notify_jmd,
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
        "5. Visitor Request"
    )


def _list_rows(*items: tuple[str, str]) -> list[dict[str, str]]:
    return [{"id": row_id, "title": title[:24]} for row_id, title in items]


def _send_main_menu(wa_id: str, employee_name: str) -> None:
    name = _chat_name(employee_name)
    rows = _list_rows(
        ("od_request", "OD Request"),
        ("vehicle_request", "Vehicle Request"),
        ("leave_request", "Leave Request"),
        ("permission_request", "Permission Request"),
        ("visitor_request", "Visitor Request"),
    )
    try:
        send_list_menu(
            wa_id_to_phone(wa_id),
            f"Welcome {name} 👋\n\nPlease choose an option:",
            rows,
            callback_data="main-menu",
        )
    except Exception:
        logger.exception("main menu InteractiveList failed to=%s", wa_id)
        _send_to(wa_id, _numbered_request_menu(employee_name))


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
        "vehicle request": "VEHICLE_REQUEST",
        "leave request": "LEAVE_REQUEST",
        "permission request": "PERMISSION_REQUEST",
        "visitor request": "VISITOR_REQUEST",
    }
    return titles.get(s.lower(), s)


def _extract_message(message_field) -> str:
    if isinstance(message_field, dict):
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
    incoming = _normalize_choice(_extract_message(raw_msg))
    logger.info(
        "parsed incoming=%s content_type=%s",
        incoming,
        msg_obj.get("message_content_type"),
    )
    return wa_id, incoming


def _process(sender: str, incoming: str) -> None:
    logger.info("process sender=%s incoming=%s", sender, incoming)
    _touch_whatsapp_inbound(sender)

    if incoming.lower() in ("hi", "hello"):
        user = db.collection("users").document(sender).get()
        if user.exists:
            name = user.to_dict().get("name", "Employee")
            _session_merge(sender, state=SESSION_MENU_IDLE, employee_name=name)
            _send_main_menu(sender, name)
        else:
            _session_ref(sender).delete()
            _send_to(sender, "User not registered.\nPlease contact admin.")
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

    if incoming == "1" or incoming == "OD_REQUEST":
        if state == SESSION_MENU_IDLE:
            od_request.try_start(sender, OD_DEPS)
        else:
            _send_to(sender, "Send Hi to start.")
        return

    if incoming == "5" or incoming == "VISITOR_REQUEST":
        if state == SESSION_MENU_IDLE:
            visitor_request.try_start(sender, VISITOR_DEPS)
        else:
            _send_to(sender, "Send Hi to start.")
        return

    if incoming in ("2", "3", "4") or incoming in _UNSUPPORTED_REQUEST_IDS:
        _send_to(sender, REQUEST_CANNOT_BE_RAISED_MSG)
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
        "runtime": "cloud_run" if _running_on_cloud_run() else "local",
        "whatsapp_session_hours": WHATSAPP_SESSION_HOURS,
        "jmd_i": JMD_I_WHATSAPP_NUMBER,
        "jmd_ii": JMD_II_WHATSAPP_NUMBER,
        "md": MD_WHATSAPP_NUMBER,
        "visitor_jmd_i": VISITOR_JMD_I_WHATSAPP_NUMBER or None,
        "visitor_md": VISITOR_MD_WHATSAPP_NUMBER or None,
        "visitor_approvers_configured": bool(
            VISITOR_JMD_I_WHATSAPP_NUMBER and VISITOR_MD_WHATSAPP_NUMBER
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
    }


@app.post("/webhook")
@app.post("/")
async def webhook(request: Request):
    body = await request.json()
    logger.info("webhook: %s", json.dumps(body, default=str)[:2500])

    parsed = _parse_webhook(body)
    if parsed:
        sender, incoming = parsed
        try:
            _process(sender, incoming)
        except Exception:
            logger.exception("process failed")

    return {"status": "success"}
