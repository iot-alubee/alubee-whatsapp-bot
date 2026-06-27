"""
Alubee WhatsApp bot (Interakt) — main entry: webhook, menu, routing.

Request flows live in separate modules:
  - od_request.py
  - visitor_request.py
  - leave_request.py
  - permission_request.py
  - it_request.py
  - vehicle_request.py
  - maintenance_request.py
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
from fastapi import FastAPI, Request
from firebase_admin import credentials, firestore
from google.api_core.exceptions import ResourceExhausted

from bot_config import bootstrap_env

import approval
import approver_availability
import bot_shared
from bot_shared import wa_from_env
import it_request
import leave_request
import vehicle_request
import maintenance_request
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
    bootstrap_env(_APP_DIR)


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
    "PERMISSION_REQUEST",
    "PERMISSION_FORM",
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
    "visitor_form": "VISITOR_FORM",
    "visitor_-_form": "VISITOR_FORM",
    "leave_form": "LEAVE_FORM",
    "leave_-_form": "LEAVE_FORM",
    "permission_form": "PERMISSION_FORM",
    "permission_-_form": "PERMISSION_FORM",
    "it_form": "IT_FORM",
    "it_-_form": "IT_FORM",
    "it_list": "IT_LIST",
    "it_-_list": "IT_LIST",
    "vehicle_request_form": "VEHICLE_REQUEST_FORM",
    "vehicle_-_request_form": "VEHICLE_REQUEST_FORM",
    "vehicle_manage": "VEHICLE_MANAGE",
    "vehicle_-_manage": "VEHICLE_MANAGE",
    "maintenance_form": "MAINTENANCE_FORM",
    "maintenance_-_form": "MAINTENANCE_FORM",
    "maintenance_manage": "MAINTENANCE_MANAGE",
    "maintenance_-_manage": "MAINTENANCE_MANAGE",
    "maintenance_list": "MAINTENANCE_LIST",
    "maintenance_-_list": "MAINTENANCE_LIST",
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
    "it_close": "IT_CLOSE",
    "it_cancel": "IT_CANCEL",
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
        _send_main_menu(sender, name, ud)
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

IT_DEPS = it_request.ItDeps(
    db=db,
    send_to=_send_to,
    session_merge=_session_merge,
    session_ref=_session_ref,
    utcnow=_utcnow,
    clear_session=lambda sender: _session_ref(sender).delete(),
    go_main_menu=_go_main_menu_for_employee,
    same_whatsapp=_same_whatsapp,
)

VEHICLE_REQUEST_DEPS = vehicle_request.VehicleRequestDeps(
    db=db,
    send_to=_send_to,
    session_merge=_session_merge,
    session_ref=_session_ref,
    utcnow=_utcnow,
    clear_session=lambda sender: _session_ref(sender).delete(),
    go_main_menu=_go_main_menu_for_employee,
    same_whatsapp=_same_whatsapp,
    has_active_whatsapp_session=_has_active_whatsapp_session,
)

MAINTENANCE_DEPS = maintenance_request.MaintenanceDeps(
    db=db,
    send_to=_send_to,
    session_merge=_session_merge,
    session_ref=_session_ref,
    utcnow=_utcnow,
    clear_session=lambda sender: _session_ref(sender).delete(),
    go_main_menu=_go_main_menu_for_employee,
    same_whatsapp=_same_whatsapp,
    has_active_whatsapp_session=_has_active_whatsapp_session,
)


def _request_menu_items(
    user_data: dict | None, wa_id: str = ""
) -> list[tuple[str, str, str]]:
    """Number, list-row id, label for main / numbered menu."""
    items: list[tuple[str, str, str]] = [
        ("1", "od_form", "OD - Form"),
        ("2", "visitor_form", "Visitor - Form"),
        ("3", "leave_form", "Leave - Form"),
    ]
    if it_request.show_it_form_for_user(user_data, wa_id, _same_whatsapp):
        items.append((str(len(items) + 1), "it_form", "IT - Form"))
    if it_request.show_it_list_menu(wa_id, _same_whatsapp):
        items.append((str(len(items) + 1), "it_list", "IT - List"))
    if maintenance_request.show_maintenance_menu_for_user(user_data):
        items.append(
            (str(len(items) + 1), "maintenance_form", "Maintenance - Form")
        )
    if maintenance_request.show_maintenance_team_list_menu(user_data):
        items.append(
            (str(len(items) + 1), "maintenance_list", "Maintenance - List")
        )
    if maintenance_request.show_maintenance_manager_menu(wa_id, _same_whatsapp):
        items.append(
            (str(len(items) + 1), "maintenance_manage", "Maintenance - Manage")
        )
    if vehicle_request.show_vehicle_menu_for_user(
        user_data, wa_id, _same_whatsapp
    ):
        if vehicle_request.is_logistics_manager(wa_id, _same_whatsapp):
            items.append(
                (str(len(items) + 1), "vehicle_manage", "Vehicle - Manage")
            )
        else:
            items.append(
                (str(len(items) + 1), "vehicle_request_form", "Vehicle - Form")
            )
    return items


def _numbered_request_menu(
    employee_name: str, user_data: dict | None = None, wa_id: str = ""
) -> str:
    name = _chat_name(employee_name)
    lines = [
        f"Welcome {name} 👋",
        "",
        "Select an option (reply with the number):",
    ]
    for num, _row_id, label in _request_menu_items(user_data, wa_id):
        lines.append(f"{num}. {label}")
    return "\n".join(lines)


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
        _send_main_menu(sender, name, ud)
    else:
        _session_merge(sender, state=SESSION_APPROVER_AVAILABILITY, approver_role=role)
    return True


def _send_main_menu(
    wa_id: str, employee_name: str, user_data: dict | None = None
) -> None:
    name = _chat_name(employee_name)
    welcome = f"Welcome {name} 👋\n\nPlease choose an option:"
    rows = _list_rows(
        *(
            (row_id, label)
            for _num, row_id, label in _request_menu_items(user_data, wa_id)
        )
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
            _send_to(wa_id, _numbered_request_menu(employee_name, user_data, wa_id))
        except Exception:
            logger.exception("numbered menu text failed to=%s", wa_id)
            _send_to(
                wa_id,
                f"{welcome}\n\nReply with the number for your request form.",
            )


def _resolve_menu_form(
    incoming: str, user_data: dict | None, wa_id: str = ""
) -> str | None:
    inc = (incoming or "").strip()
    inc_upper = inc.upper()
    for num, row_id, _label in _request_menu_items(user_data, wa_id):
        form_id = _ROW_IDS.get(row_id, row_id.upper()).upper()
        if inc == num or inc_upper == form_id or inc.lower() == row_id.lower():
            return form_id
    return None


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
        "visitor request": "VISITOR_REQUEST",
        "visitor - form": "VISITOR_FORM",
        "visitor form": "VISITOR_FORM",
        "leave - form": "LEAVE_FORM",
        "leave form": "LEAVE_FORM",
        "it - form": "IT_FORM",
        "it form": "IT_FORM",
        "it - list": "IT_LIST",
        "it list": "IT_LIST",
        "vehicle - form": "VEHICLE_REQUEST_FORM",
        "vehicle form": "VEHICLE_REQUEST_FORM",
        "vehicle request form": "VEHICLE_REQUEST_FORM",
        "maintenance - form": "MAINTENANCE_FORM",
        "maintenance form": "MAINTENANCE_FORM",
        "maintenance - manage": "MAINTENANCE_MANAGE",
        "maintenance manage": "MAINTENANCE_MANAGE",
        "maintenance - list": "MAINTENANCE_LIST",
        "maintenance list": "MAINTENANCE_LIST",
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


def _as_dict_maybe(value) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("{"):
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _flow_response_from_interactive(reply) -> dict | str | None:
    block = _as_dict_maybe(reply) or {}
    if not block:
        return None
    return _coerce_flow_response(
        block.get("response_json")
        or block.get("response")
        or block.get("payload")
        or block.get("body")
    )


def _looks_like_flow_payload(payload) -> bool:
    if not isinstance(payload, dict):
        return bool(payload)
    keys = {str(k).lower() for k in payload.keys()}
    return bool(
        keys
        & {
            "od_reason",
            "company_vehicle",
            "vehicle",
            "coming_on",
            "coming_from",
            "purpose",
            "visitor_name",
            "leave_when",
            "leave_reason",
            "permission_for",
            "permission_type",
            "it_category",
            "issue_type",
            "issue_photo",
        }
    )


def _deep_find_flow_response(obj, depth: int = 0) -> dict | str | None:
    if depth > 12:
        return None
    if isinstance(obj, dict):
        if obj.get("type") == "nfm_reply":
            nfm = obj.get("nfm_reply") or {}
            found = _coerce_flow_response(
                nfm.get("response_json") or nfm.get("response") or nfm.get("body")
            )
            if found:
                return found
        found = _flow_response_from_interactive(obj.get("interactive_flow_reply"))
        if found:
            return found
        for key in ("response_json", "response", "flow_response", "body"):
            if key in obj:
                found = _coerce_flow_response(obj[key])
                if found and _looks_like_flow_payload(found):
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
    elif isinstance(obj, str):
        nested = _as_dict_maybe(obj)
        if nested:
            found = _deep_find_flow_response(nested, depth + 1)
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
    if callback in ("leave-flow", "leave_form", "leave"):
        return "leave-flow"
    if callback in ("permission-flow", "permission_form", "permission"):
        return "permission-flow"
    if callback in ("it-flow", "it_form", "it"):
        return "it-flow"
    if callback in ("maintenance-flow", "maintenance_form", "maintenance"):
        return "maintenance-flow"
    if callback in ("vehicle-request-flow", "vehicle_request_form", "vehicle_request"):
        return "vehicle-request-flow"
    return callback


def _is_flow_reply_webhook(body: dict) -> bool:
    wtype = (body.get("type") or "").strip().lower()
    if wtype == "message_api_flow_response":
        return True
    if _flow_callback_kind(body) in (
        "od-flow",
        "visitor-flow",
        "leave-flow",
        "permission-flow",
        "it-flow",
        "maintenance-flow",
        "vehicle-request-flow",
    ):
        return True
    data = body.get("data") or {}
    msg_obj = data.get("message") or {}
    raw_msg = _as_dict_maybe(msg_obj.get("message")) or {}
    if raw_msg.get("type") == "nfm_reply":
        return True
    if raw_msg.get("interactive_flow_reply") or msg_obj.get("interactive_flow_reply"):
        return True
    content_type = (msg_obj.get("message_content_type") or "").strip().lower()
    if content_type in ("interactiveflowreply", "flow", "nfm_reply"):
        return True
    if _deep_find_flow_response(body):
        return True
    return False


def _is_interactive_flow_message(message_field) -> bool:
    block = _as_dict_maybe(message_field)
    if not block:
        return False
    return bool(block.get("interactive_flow_reply")) or block.get("type") in (
        "nfm_reply",
        "interactive",
    )


def _extract_message(message_field) -> str:
    if _is_interactive_flow_message(message_field):
        return ""
    if isinstance(message_field, dict):
        if message_field.get("type") == "nfm_reply":
            return ""
        if message_field.get("type") == "list_reply":
            lr = message_field.get("list_reply") or {}
            if lr.get("id"):
                return str(lr["id"])
        if message_field.get("type") == "button_reply":
            br = message_field.get("button_reply") or {}
            bid = str(br.get("id") or "").strip()
            btitle = str(br.get("title") or "").strip()
            if bid.upper().startswith(
                (
                    "APPROVE_", "DENY_", "MANAGE_", "CLARITY_", "VEHICLE_", "VMANAGE_",
                    "VMREASSIGN_", "VMCANCEL_", "VREASSIGN_", "VASSIGN_",
                    "MMAINT_", "MTEAM_", "MMANAGE_", "MASSIGN_",
                    "ITM_", "ITMGR_", "ITLIST_", "ITASSIGN_", "ITENG_", "ITCLOSED_",
                    "ITUSER_CLOSE_",
                )
            ):
                return bid
            if btitle:
                return btitle
            if bid:
                return bid
        br = message_field.get("button_reply")
        if isinstance(br, dict):
            bid = str(br.get("id") or "").strip()
            btitle = str(br.get("title") or "").strip()
            if bid.upper().startswith(
                (
                    "APPROVE_", "DENY_", "MANAGE_", "CLARITY_", "VEHICLE_", "VMANAGE_",
                    "VMREASSIGN_", "VMCANCEL_", "VREASSIGN_", "VASSIGN_",
                    "MMAINT_", "MTEAM_", "MMANAGE_", "MASSIGN_",
                    "ITM_", "ITMGR_", "ITLIST_", "ITASSIGN_", "ITENG_", "ITCLOSED_",
                    "ITUSER_CLOSE_",
                )
            ):
                return bid
            if btitle:
                return btitle
            if bid:
                return bid
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


def _callback_from_meta(meta: object) -> str:
    if not isinstance(meta, dict):
        return ""
    for key in ("callback_data", "callbackData"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            nested = _callback_from_meta(val)
            if nested:
                return nested
    source = meta.get("source_data")
    if isinstance(source, dict):
        nested = _callback_from_meta(source)
        if nested:
            return nested
    return ""


def _extract_webhook_callback_request_id(body: dict) -> str:
    """Request id echoed when approver taps a template quick-reply (callbackData)."""
    data = body.get("data") or {}
    msg_obj = data.get("message") or {}
    for candidate in (body, data, msg_obj):
        if not isinstance(candidate, dict):
            continue
        for key in ("callbackData", "callback_data"):
            val = (candidate.get(key) or "").strip()
            if val:
                return val
        rid = _callback_from_meta(candidate.get("meta_data"))
        if rid:
            return rid
    return ""


def _flow_response_from_message(msg_obj: dict) -> dict | str | None:
    """Extract flow submit payload from Interakt message_received webhooks."""
    found = _flow_response_from_interactive(msg_obj.get("interactive_flow_reply"))
    if found:
        return found

    raw_msg = _as_dict_maybe(msg_obj.get("message"))
    if not raw_msg:
        return None

    found = _flow_response_from_interactive(raw_msg.get("interactive_flow_reply"))
    if found:
        return found

    if raw_msg.get("type") == "nfm_reply":
        nfm = raw_msg.get("nfm_reply") or {}
        return _coerce_flow_response(
            nfm.get("response_json") or nfm.get("response") or nfm.get("payload")
        )
    return None


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
        elif keys & {"leave_when", "leave_reason", "from_date"}:
            callback = "leave-flow"
        elif keys & {"permission_for", "permission_type", "permission_shift"}:
            callback = "permission-flow"
        elif keys & {"it_category", "issue_type"}:
            callback = "it-flow"
        elif keys & {"machine_type", "machine_no", "issue_category"}:
            callback = "maintenance-flow"
        elif keys & {"request_type", "destination_category", "load_size"}:
            callback = "vehicle-request-flow"
    return phone_to_wa_id(phone), response, callback or "flow"


def _parse_webhook(body: dict) -> tuple[str, str, str] | None:
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
    if _is_interactive_flow_message(raw_msg):
        return None
    incoming = _normalize_choice(_extract_message(raw_msg))
    callback_rid = _extract_webhook_callback_request_id(body)
    logger.info(
        "parsed incoming=%s callback_rid=%s content_type=%s",
        incoming,
        callback_rid or "(none)",
        msg_obj.get("message_content_type"),
    )
    return wa_id, incoming, callback_rid


def _is_configured_approver(sender: str) -> bool:
    return bool(
        approver_availability.approver_role_for_sender(
            sender,
            md=MD_WHATSAPP_NUMBER,
            jmd_i=JMD_I_WHATSAPP_NUMBER,
            jmd_ii=JMD_II_WHATSAPP_NUMBER,
            same_whatsapp=_same_whatsapp,
            test_md=TEST_MD_WHATSAPP_NUMBER,
        )
    )


def _is_special_operator(sender: str) -> bool:
    """JMD/MD approvers or logistics manager — not regular employees."""
    if _is_configured_approver(sender):
        return True
    return vehicle_request.is_logistics_manager(sender, _same_whatsapp)


def _process(
    sender: str,
    incoming: str,
    *,
    callback_request_id: str = "",
) -> None:
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
        if vehicle_request.is_logistics_manager(sender, _same_whatsapp):
            exists, ud = bot_shared.get_user_record(sender)
            name = _chat_name((ud or {}).get("name") or "Logistics Manager")
            _session_merge(sender, state=SESSION_MENU_IDLE, employee_name=name)
            _send_main_menu(sender, name, ud if exists else {"name": name})
            return
        if exists and ud:
            name = ud.get("name", "Employee")
            _session_merge(sender, state=SESSION_MENU_IDLE, employee_name=name)
            _send_main_menu(sender, name, ud)
        else:
            _session_ref(sender).delete()
            _send_to(sender, "User not registered.\nPlease contact admin.")
        return

    _touch_whatsapp_inbound(sender)

    if _try_handle_approver_availability(sender, incoming):
        return

    if approval.handle_md_clarity_gate(
        sender, incoming, callback_request_id=callback_request_id
    ):
        return

    if approval.handle_employee_clarity_reply_gate(
        sender, incoming, callback_request_id=callback_request_id
    ):
        return

    if approval.handle_leave_manage_gate(
        sender, incoming, callback_request_id=callback_request_id
    ):
        return

    if it_request.handle_it_user_close_gate(
        sender,
        incoming,
        IT_DEPS,
        callback_request_id=callback_request_id,
    ):
        return

    if it_request.handle_it_engineer_close_gate(
        sender,
        incoming,
        IT_DEPS,
        callback_request_id=callback_request_id,
    ):
        return

    if it_request.handle_it_engineer_list_gate(
        sender,
        incoming,
        IT_DEPS,
        callback_request_id=callback_request_id,
    ):
        return

    if it_request.handle_it_manager_gate(
        sender,
        incoming,
        IT_DEPS,
        callback_request_id=callback_request_id,
    ):
        return

    if it_request.handle_it_manager_list_gate(
        sender,
        incoming,
        IT_DEPS,
        callback_request_id=callback_request_id,
    ):
        return

    if maintenance_request.handle_team_list_gate(
        sender,
        incoming,
        MAINTENANCE_DEPS,
        callback_request_id=callback_request_id,
    ):
        return

    if maintenance_request.handle_maintenance_assignee_gate(
        sender,
        incoming,
        MAINTENANCE_DEPS,
        callback_request_id=callback_request_id,
    ):
        return

    if vehicle_request.handle_assignee_gate(sender, incoming, VEHICLE_REQUEST_DEPS):
        return

    if maintenance_request.handle_manager_manage_gate(
        sender,
        incoming,
        MAINTENANCE_DEPS,
        callback_request_id=callback_request_id,
    ):
        return

    if maintenance_request.handle_maintenance_manager_gate(
        sender,
        incoming,
        MAINTENANCE_DEPS,
        callback_request_id=callback_request_id,
    ):
        return

    if vehicle_request.handle_manager_manage_gate(
        sender,
        incoming,
        VEHICLE_REQUEST_DEPS,
        callback_request_id=callback_request_id,
    ):
        return

    if vehicle_request.handle_logistics_manager_gate(
        sender,
        incoming,
        VEHICLE_REQUEST_DEPS,
        callback_request_id=callback_request_id,
    ):
        return

    if approval.handle_approval_gate(
        sender, incoming, callback_request_id=callback_request_id
    ):
        return

    session_doc = _session_ref(sender).get()
    session = session_doc.to_dict() if session_doc.exists else None
    state = (session or {}).get("state")

    if approval.is_md_clarity_state(state):
        approval.handle_md_clarity_input(sender, incoming, session or {})
        return

    if approval.is_employee_clarity_state(state):
        approval.handle_employee_clarity_input(sender, incoming, session or {})
        return

    if approval.is_leave_manage_state(state):
        approval.handle_leave_manage_input(sender, incoming, session or {})
        return

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
        _session_ref(sender).delete()
        _send_to(sender, REQUEST_CANNOT_BE_RAISED_MSG)
        return

    if it_request.is_it_manager_notify_state(state):
        if it_request.handle_it_manager_notify_input(
            sender,
            incoming,
            session or {},
            IT_DEPS,
            callback_request_id=callback_request_id,
        ):
            return
        _send_to(
            sender,
            "Tap Assign on the IT ticket, or send IT - List to choose a request.",
        )
        return

    if it_request.is_it_manager_reassign_state(state):
        it_request.handle_it_manager_assign_pick(
            sender, incoming, session or {}, IT_DEPS
        )
        return

    if it_request.is_it_manager_assign_state(state):
        it_request.handle_it_manager_assign_pick(
            sender, incoming, session or {}, IT_DEPS
        )
        return

    if maintenance_request.is_maintenance_reassign_state(state):
        maintenance_request.handle_maintenance_reassign_pick(
            sender, incoming, session or {}, MAINTENANCE_DEPS
        )
        return

    if maintenance_request.is_maintenance_manage_action_state(state):
        maintenance_request.handle_manager_manage_action(
            sender,
            incoming,
            session or {},
            MAINTENANCE_DEPS,
            callback_request_id=callback_request_id,
        )
        return

    if maintenance_request.is_maintenance_assign_state(state):
        maintenance_request.handle_maintenance_assign_pick(
            sender, incoming, session or {}, MAINTENANCE_DEPS
        )
        return

    if vehicle_request.is_vehicle_reassign_state(state):
        vehicle_request.handle_vehicle_reassign_pick(
            sender, incoming, session or {}, VEHICLE_REQUEST_DEPS
        )
        return

    if vehicle_request.is_vehicle_manage_action_state(state):
        vehicle_request.handle_manager_manage_action(
            sender,
            incoming,
            session or {},
            VEHICLE_REQUEST_DEPS,
            callback_request_id=callback_request_id,
        )
        return

    if vehicle_request.is_vehicle_assign_type_state(state):
        vehicle_request.handle_vehicle_assign_type_pick(
            sender,
            incoming,
            session or {},
            VEHICLE_REQUEST_DEPS,
            callback_request_id=callback_request_id,
        )
        return

    if vehicle_request.is_vehicle_assign_type_state(state):
        vehicle_request.handle_vehicle_assign_type_pick(
            sender,
            incoming,
            session or {},
            VEHICLE_REQUEST_DEPS,
            callback_request_id=callback_request_id,
        )
        return

    if vehicle_request.is_vehicle_fleet_pick_state(state):
        vehicle_request.handle_vehicle_fleet_pick(
            sender, incoming, session or {}, VEHICLE_REQUEST_DEPS
        )
        return

    if vehicle_request.is_vehicle_reassign_fleet_pick_state(state):
        vehicle_request.handle_vehicle_reassign_fleet_pick(
            sender, incoming, session or {}, VEHICLE_REQUEST_DEPS
        )
        return

    if vehicle_request.is_vehicle_assign_state(state):
        vehicle_request.handle_vehicle_assign_pick(
            sender, incoming, session or {}, VEHICLE_REQUEST_DEPS
        )
        return

    exists, ud = bot_shared.get_user_record(sender)
    user_data = ud if exists else None
    menu_form = _resolve_menu_form(incoming, user_data, sender)
    if menu_form:
        if state != SESSION_MENU_IDLE:
            _send_to(sender, "Send Hi to start.")
            return
        if menu_form == "OD_FORM":
            od_request.try_start_form(sender, OD_DEPS)
        elif menu_form == "VISITOR_FORM":
            visitor_request.try_start_form(sender, VISITOR_DEPS)
        elif menu_form == "LEAVE_FORM":
            leave_request.try_start_form(sender, LEAVE_DEPS)
        elif menu_form == "IT_FORM":
            it_request.try_start_form(sender, IT_DEPS)
        elif menu_form == "IT_LIST":
            it_request.try_start_it_list(sender, IT_DEPS)
        elif menu_form == "MAINTENANCE_FORM":
            maintenance_request.try_start_form(sender, MAINTENANCE_DEPS)
        elif menu_form == "MAINTENANCE_MANAGE":
            maintenance_request.try_start_manage(sender, MAINTENANCE_DEPS)
        elif menu_form == "MAINTENANCE_LIST":
            maintenance_request.try_start_team_list(sender, MAINTENANCE_DEPS)
        elif menu_form == "VEHICLE_MANAGE":
            vehicle_request.try_start_manage(sender, VEHICLE_REQUEST_DEPS)
        elif menu_form == "VEHICLE_REQUEST_FORM":
            vehicle_request.try_start_form(sender, VEHICLE_REQUEST_DEPS)
        return

    if incoming in ("PERMISSION_REQUEST", "PERMISSION_FORM"):
        _send_to(sender, REQUEST_CANNOT_BE_RAISED_MSG)
        return

    if incoming == "OD_REQUEST":
        if state == SESSION_MENU_IDLE:
            od_request.try_start(sender, OD_DEPS)
        else:
            _send_to(sender, "Send Hi to start.")
        return

    if incoming == "LEAVE_REQUEST":
        if state == SESSION_MENU_IDLE:
            leave_request.try_start(sender, LEAVE_DEPS)
        else:
            _send_to(sender, "Send Hi to start.")
        return

    if incoming == "VISITOR_REQUEST":
        if state == SESSION_MENU_IDLE:
            visitor_request.try_start(sender, VISITOR_DEPS)
        else:
            _send_to(sender, "Send Hi to start.")
        return

    if incoming in _UNSUPPORTED_REQUEST_IDS:
        _send_to(sender, REQUEST_CANNOT_BE_RAISED_MSG)
        return

    if not incoming.strip():
        return

    if session_doc.exists:
        _send_to(sender, "Invalid session state")
        return

    if _is_special_operator(sender):
        logger.info(
            "ignored operator message sender=%s incoming=%s callback_rid=%s",
            sender,
            incoming,
            callback_request_id or "(none)",
        )
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
        "visitor_form_configured": visitor_request.visitor_flow_enabled(),
        "visitor_flow_template": visitor_request.visitor_flow_template_name(),
        "leave_form_configured": leave_request.leave_flow_enabled(),
        "leave_flow_template": leave_request.leave_flow_template_name(),
        "permission_form_configured": permission_request.permission_flow_enabled(),
        "permission_flow_template": permission_request.permission_flow_template_name(),
        "it_form_configured": it_request.it_flow_enabled(),
        "it_flow_template": it_request.it_flow_template_name(),
        "vehicle_request_form_configured": vehicle_request.vehicle_request_flow_enabled(),
        "vehicle_request_flow_template": vehicle_request.vehicle_request_flow_template_name(),
        "maintenance_form_configured": maintenance_request.maintenance_flow_enabled(),
        "maintenance_flow_template": maintenance_request.maintenance_flow_template_name(),
    }


@app.post("/webhook")
@app.post("/")
async def webhook(request: Request):
    body = await request.json()
    logger.info("webhook: %s", json.dumps(body, default=str)[:2500])

    flow_parsed = _parse_flow_webhook(body)
    if flow_parsed:
        sender, response_json, flow_kind = flow_parsed
        logger.info(
            "flow submit parsed sender=%s kind=%s keys=%s",
            sender,
            flow_kind,
            list(response_json.keys()) if isinstance(response_json, dict) else type(response_json).__name__,
        )
        if flow_kind in ("od-flow", "od_form", "od"):
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
        elif flow_kind in ("visitor-flow", "visitor_form", "visitor"):
            try:
                visitor_request.handle_flow_submission(sender, response_json, VISITOR_DEPS)
            except Exception:
                logger.exception("visitor flow submit failed sender=%s", sender)
                try:
                    _send_to(
                        sender,
                        "Sorry, we could not save your visitor form. Please send Hi and try again.",
                    )
                except Exception:
                    logger.exception("could not notify user after visitor flow error")
        elif flow_kind in ("leave-flow", "leave_form", "leave"):
            try:
                leave_request.handle_flow_submission(sender, response_json, LEAVE_DEPS)
            except Exception:
                logger.exception("leave flow submit failed sender=%s", sender)
                try:
                    _send_to(
                        sender,
                        "Sorry, we could not save your leave form. Please send Hi and try again.",
                    )
                except Exception:
                    logger.exception("could not notify user after leave flow error")
        elif flow_kind in ("permission-flow", "permission_form", "permission"):
            logger.info("permission flow submit ignored (disabled) sender=%s", sender)
            try:
                _send_to(sender, REQUEST_CANNOT_BE_RAISED_MSG)
            except Exception:
                pass
        elif flow_kind in ("it-flow", "it_form", "it"):
            try:
                it_request.handle_flow_submission(sender, response_json, IT_DEPS)
            except Exception:
                logger.exception("IT flow submit failed sender=%s", sender)
                try:
                    _send_to(
                        sender,
                        "Sorry, we could not save your IT form. Please send Hi and try again.",
                    )
                except Exception:
                    logger.exception("could not notify user after IT flow error")
        elif flow_kind in (
            "vehicle-request-flow",
            "vehicle_request_form",
            "vehicle_request",
            "logistics-flow",
            "logistics_form",
            "logistics",
        ):
            try:
                vehicle_request.handle_flow_submission(
                    sender, response_json, VEHICLE_REQUEST_DEPS
                )
            except Exception:
                logger.exception("vehicle request flow submit failed sender=%s", sender)
                try:
                    _send_to(
                        sender,
                        "Sorry, we could not save your Vehicle Request form. Please send Hi and try again.",
                    )
                except Exception:
                    logger.exception("could not notify user after vehicle request flow error")
        elif flow_kind in ("maintenance-flow", "maintenance_form", "maintenance"):
            try:
                maintenance_request.handle_flow_submission(
                    sender, response_json, MAINTENANCE_DEPS
                )
            except Exception:
                logger.exception("maintenance flow submit failed sender=%s", sender)
                try:
                    _send_to(
                        sender,
                        "Sorry, we could not save your Maintenance form. Please send Hi and try again.",
                    )
                except Exception:
                    logger.exception("could not notify user after maintenance flow error")
        elif flow_kind == "flow" and isinstance(response_json, dict):
            keys = {str(k).lower() for k in response_json.keys()}
            if keys & {"od_reason", "company_vehicle", "vehicle"}:
                try:
                    od_request.handle_flow_submission(sender, response_json, OD_DEPS)
                except Exception:
                    logger.exception("OD flow submit failed sender=%s", sender)
            elif keys & {"coming_on", "coming_from", "visitor_name", "visitor_mobile"}:
                try:
                    visitor_request.handle_flow_submission(sender, response_json, VISITOR_DEPS)
                except Exception:
                    logger.exception("visitor flow submit failed sender=%s", sender)
            elif keys & {"leave_when", "leave_reason"}:
                try:
                    leave_request.handle_flow_submission(sender, response_json, LEAVE_DEPS)
                except Exception:
                    logger.exception("leave flow submit failed sender=%s", sender)
            elif keys & {"permission_for", "reason"}:
                logger.info("permission flow submit ignored (disabled) sender=%s", sender)
                try:
                    _send_to(sender, REQUEST_CANNOT_BE_RAISED_MSG)
                except Exception:
                    pass
            elif keys & {"it_category", "issue_type"}:
                try:
                    it_request.handle_flow_submission(sender, response_json, IT_DEPS)
                except Exception:
                    logger.exception("IT flow submit failed sender=%s", sender)
            elif keys & {"machine_type", "machine_no", "issue_category"}:
                try:
                    maintenance_request.handle_flow_submission(
                        sender, response_json, MAINTENANCE_DEPS
                    )
                except Exception:
                    logger.exception("maintenance flow submit failed sender=%s", sender)
            elif keys & {"request_type", "destination_category", "load_size"}:
                try:
                    vehicle_request.handle_flow_submission(
                        sender, response_json, VEHICLE_REQUEST_DEPS
                    )
                except Exception:
                    logger.exception("vehicle request flow submit failed sender=%s", sender)
            else:
                logger.info("ignored flow submit kind=%s sender=%s", flow_kind, sender)
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
        sender, incoming, callback_rid = parsed
        try:
            for attempt in range(2):
                try:
                    _process(
                        sender,
                        incoming,
                        callback_request_id=callback_rid,
                    )
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
