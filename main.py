"""
Alubee WhatsApp OD bot — Interakt InteractiveList (menu) + buttons + text vehicle list.

Flow: Hi → list menu → OD reason buttons → company vehicle → numbered text vehicles → approval.
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
from fastapi.responses import JSONResponse
from firebase_admin import credentials, firestore

from interakt_api import (
    ensure_customer,
    phone_to_wa_id,
    send_list_menu,
    send_reply_buttons,
    send_text,
    wa_id_to_phone,
)

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

# Approver numbers (10-digit mobiles → whatsapp:+91…)
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

# WhatsApp session window for outbound session messages (JMD/MD approval).
WHATSAPP_SESSION_HOURS = int(os.getenv("WHATSAPP_SESSION_HOURS", "24"))

REQUEST_CANNOT_BE_RAISED_MSG = "This request cannot be raised now. Thanks!"

OD_ALREADY_PENDING_MSG = "Your OD request is already pending."

_UNSUPPORTED_REQUEST_IDS = frozenset({
    "VEHICLE_REQUEST",
    "LEAVE_REQUEST",
    "PERMISSION_REQUEST",
    "VISITOR_REQUEST",
})

_OD_SESSION_STATES = frozenset({
    "WAITING_OD_REASON_PICK",
    "WAITING_OD_REASON_TYPING",
    "WAITING_COMPANY_VEHICLE_YESNO",
    "WAITING_VEHICLE_PICK",
    "WAITING_OD_CONFIRM",
})

_SESSION_MENU_IDLE = "MENU_IDLE"
_SESSION_AWAITING_HI = "AWAITING_HI"

# List row id (lowercase) → internal choice
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

_OD_REASON_CHOICES = frozenset({"UNIT_I", "UNIT_II", "OTHER"})
_COMPANY_VEHICLE_CHOICES = frozenset({"YES", "NO"})
_CONFIRM_CHOICES = frozenset({"SUBMIT", "CANCEL", "BACK"})


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

app = FastAPI(title="Alubee Interakt OD bot")


def _utcnow():
    return datetime.now(timezone.utc)


def _session_ref(sender: str):
    return db.collection("sessions").document(sender)


def _whatsapp_activity_ref(wa_id: str):
    return db.collection("whatsapp_activity").document(wa_id)


def _touch_whatsapp_inbound(wa_id: str) -> None:
    """Record last inbound message (opens 24h session for outbound session messages)."""
    _whatsapp_activity_ref(wa_id).set({"last_inbound_at": _utcnow()}, merge=True)


def _has_active_whatsapp_session(wa_id: str) -> bool:
    """True if this user messaged Alubee within WHATSAPP_SESSION_HOURS."""
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


def _same_whatsapp(a: str, b: str) -> bool:
    return bool(a and b and a.strip().lower() == b.strip().lower())


def _send_to(wa_id: str, text: str) -> None:
    try:
        send_text(wa_id_to_phone(wa_id), text)
    except Exception:
        logger.exception("send_text failed to=%s", wa_id)


def _list_rows(*items: tuple[str, str]) -> list[dict[str, str]]:
    """InteractiveList rows: (id, title) only — no descriptions."""
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


def _send_od_reason_buttons(wa_id: str) -> None:
    """InteractiveList: Unit I / Unit II / Other / Back."""
    rows = _list_rows(
        ("unit_i", "Unit I"),
        ("unit_ii", "Unit II"),
        ("other", "Other"),
        ("back", "Back"),
    )
    try:
        send_list_menu(
            wa_id_to_phone(wa_id),
            "OD reason:",
            rows,
            button_label="Select reason",
            section_title="Reason",
            callback_data="od-reason",
        )
    except Exception:
        logger.exception("OD reason list failed")
        _send_to(
            wa_id,
            "OD reason:\n1. Unit I\n2. Unit II\n3. Other\n\nReply 1, 2, 3, or BACK for menu.",
        )


def _send_company_vehicle_buttons(wa_id: str, reason: str) -> None:
    """YES / NO / Back."""
    body = f"{reason}\n\nCompany vehicle?"
    try:
        send_reply_buttons(
            wa_id_to_phone(wa_id),
            body,
            [("YES", "YES"), ("NO", "NO"), ("BACK", "Back")],
            callback_data="company-vehicle",
        )
    except Exception:
        logger.exception("company vehicle buttons failed")
        _send_to(wa_id, f"{body}\n\nReply YES, NO, or BACK.")


def _send_dynamic_vehicle_list(wa_id: str, vehicles: list) -> None:
    """Dynamic numbered text list from Firestore (all vehicles; reply 1–N or vehicle ID)."""
    n = len(vehicles)
    if n == 0:
        return
    lines = [f"Select company vehicle ({n} available; reply with number or ID):\n"]
    for i, v in enumerate(vehicles, start=1):
        label = v.get("description") or v.get("vehicle_id")
        lines.append(f"{i}. {label} ({v['vehicle_id']})")
    lines.append("\nReply BACK to go back.")
    _send_to(wa_id, "\n".join(lines))


def _send_approval_buttons(
    wa_id: str,
    *,
    employee_name: str,
    department: str,
    reason: str,
    request_id: str,
) -> bool:
    """Send Approve/Deny only if approver has an active WhatsApp session (messaged Alubee recently)."""
    if not _has_active_whatsapp_session(wa_id):
        logger.info(
            "skip approval notify to=%s request_id=%s (no active WhatsApp session in %sh)",
            wa_id,
            request_id,
            WHATSAPP_SESSION_HOURS,
        )
        return False

    body = (
        "OD approval request\n\n"
        f"Employee: {_chat_name(employee_name)}\n"
        f"Department: {department or '—'}\n"
        f"Reason: {reason or '—'}\n\n"
        "Please approve or deny."
    )
    try:
        send_reply_buttons(
            wa_id_to_phone(wa_id),
            body,
            [("APPROVE", "Approve"), ("DENY", "Deny")],
            callback_data=request_id,
            ensure_contact=True,
            contact_name=_chat_name(employee_name),
        )
        return True
    except Exception as e:
        logger.exception("approval buttons failed to=%s: %s", wa_id, e)
        try:
            ensure_customer(wa_id_to_phone(wa_id), name="Approver")
            send_reply_buttons(
                wa_id_to_phone(wa_id),
                body,
                [("APPROVE", "Approve"), ("DENY", "Deny")],
                callback_data=request_id,
            )
            return True
        except Exception:
            logger.exception("approval retry failed to=%s", wa_id)
        return False


def _set_pending_approval(recipient: str, request_id: str) -> None:
    _session_merge(
        recipient,
        state="WAITING_APPROVAL_ACTION",
        approval_request_id=request_id,
    )


def _resolve_approval(incoming: str, approver: str):
    raw = (incoming or "").strip()
    um = raw.upper()
    if um.startswith("APPROVE_"):
        rid = raw.split("_", 1)[1].strip()
        return (True, rid) if rid else (None, None)
    if um.startswith("DENY_"):
        rid = raw.split("_", 1)[1].strip()
        return (False, rid) if rid else (None, None)
    if um in ("APPROVE", "DENY"):
        snap = db.collection("sessions").document(approver).get()
        if snap.exists:
            data = snap.to_dict() or {}
            if data.get("state") == "WAITING_APPROVAL_ACTION":
                rid = (data.get("approval_request_id") or "").strip()
                if rid:
                    return um == "APPROVE", rid
    return None, None


def _jmd_route_from_names(mgr_l1_name: str, mgr_l2_name: str) -> str:
    """JMD1 or JMD2 from spreadsheet name columns (L2 checked first, then L1)."""
    for name in (mgr_l2_name, mgr_l1_name):
        key = (name or "").strip().upper().replace(" ", "")
        if key == "JMD2":
            return "JMD2"
        if key == "JMD1":
            return "JMD1"
    return "JMD1"


def _jmd_whatsapp_for_route(jmd_route: str) -> str:
    if (jmd_route or "").strip().upper() == "JMD2":
        return JMD_II_WHATSAPP_NUMBER
    return JMD_I_WHATSAPP_NUMBER


def _request_jmd_whatsapp(rd: dict) -> str:
    """Resolve JMD WhatsApp from jmd_route (env), not a stale stored jmd number."""
    route = (rd.get("jmd_route") or "").strip().upper()
    if route in ("JMD1", "JMD2"):
        return _jmd_whatsapp_for_route(route)
    stored = (rd.get("jmd") or "").strip()
    if stored:
        return stored
    return JMD_I_WHATSAPP_NUMBER


def _build_approval_chain(user_data: dict | None = None) -> dict | None:
    """Employee → JMD1 or JMD2 (per employee) → MD (final)."""
    if not user_data:
        return None
    jmd_route = (user_data.get("jmd_route") or "JMD1").strip().upper()
    jmd = _jmd_whatsapp_for_route(jmd_route)
    md = MD_WHATSAPP_NUMBER
    if not jmd or not md:
        return None
    return {
        "jmd": jmd,
        "jmd_route": jmd_route,
        "md": md,
    }


def _approval_role(sender: str, rd: dict) -> str | None:
    """JMD (I or II on request) → MD (final)."""
    jmd_st = (rd.get("jmd_status") or "").strip().upper()
    md_st = (rd.get("md_status") or "").strip().upper()
    jmd_wa = _request_jmd_whatsapp(rd)

    if _same_whatsapp(sender, jmd_wa) and jmd_st in ("PENDING", "AWAITING_MANAGER"):
        return "jmd"

    if (
        _same_whatsapp(sender, MD_WHATSAPP_NUMBER)
        and jmd_st == "APPROVED"
        and md_st == "PENDING"
    ):
        return "md"

    return None


def _notify_approver(wa_id: str, rd: dict, request_id: str) -> None:
    if not wa_id:
        return
    if _send_approval_buttons(
        wa_id,
        employee_name=rd.get("employee_name"),
        department=rd.get("department"),
        reason=rd.get("reason"),
        request_id=request_id,
    ):
        _set_pending_approval(wa_id, request_id)


def _handle_approval_gate(sender: str, incoming: str) -> bool:
    resolved = _resolve_approval(incoming, sender)
    if resolved[0] is None:
        return False

    is_approve, request_id = resolved
    ref = db.collection("requests").document(request_id)
    snap = ref.get()
    if not snap.exists:
        logger.warning("request not found %s", request_id)
        return True

    rd = snap.to_dict()
    employee = rd.get("employee")
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
            ref.update({
                "jmd": _request_jmd_whatsapp(rd),
                "jmd_route": (rd.get("jmd_route") or "JMD1").strip().upper(),
                "md": MD_WHATSAPP_NUMBER,
                "manager_status": "N/A",
                "jmd_status": "APPROVED",
                "md_status": "PENDING",
            })
            _notify_approver(MD_WHATSAPP_NUMBER, rd, request_id)
            logger.info("jmd approved request_id=%s → md", request_id)
        else:
            ref.update({
                "manager_status": "N/A",
                "jmd_status": "DENIED",
                "md_status": "N/A",
            })
            _send_to(employee, "Your OD request was not approved.")
            logger.info("jmd denied request_id=%s", request_id)

    elif role == "md":
        if is_approve:
            patch = {
                "md_status": "APPROVED",
                "approved_datetime": _utcnow(),
            }
            if (rd.get("jmd_status") or "").strip().upper() in (
                "PENDING",
                "AWAITING_MANAGER",
            ):
                patch["jmd_status"] = "APPROVED"
            ref.update(patch)
            _send_to(employee, "Your OD has been Approved.")
            logger.info("md approved (final) request_id=%s", request_id)
        else:
            ref.update({
                "manager_status": "N/A",
                "jmd_status": "DENIED",
                "md_status": "DENIED",
            })
            _send_to(employee, "Your OD request was not approved.")
            logger.info("md denied (final) request_id=%s", request_id)

    _session_ref(sender).delete()
    return True


def _od_request_is_closed(d: dict) -> bool:
    """Open until JMD/MD denies or security records IN (visit closed)."""
    for key in ("manager_status", "jmd_status", "md_status"):
        if (d.get(key) or "").strip().upper() == "DENIED":
            return True
    if d.get("security_in_at"):
        return True
    return False


def _find_open_od_for_employee(employee: str) -> dict | None:
    for snap in db.collection("requests").stream():
        d = snap.to_dict() or {}
        if (d.get("type") or "").strip().upper() != "OD":
            continue
        if not _same_whatsapp(d.get("employee"), employee):
            continue
        if _od_request_is_closed(d):
            continue
        return d
    return None


def _try_start_od(sender: str) -> None:
    if _find_open_od_for_employee(sender):
        _send_to(sender, OD_ALREADY_PENDING_MSG)
        return
    _start_od(sender)


def _vehicles_out_ids():
    out = set()
    for snap in db.collection("requests").stream():
        d = snap.to_dict() or {}
        if (d.get("type") or "").upper() != "OD":
            continue
        vid = (d.get("company_vehicle_id") or "").strip().upper()
        if vid and d.get("security_out_at") and not d.get("security_in_at"):
            out.add(vid)
    return out


def _fetch_vehicles():
    out_ids = _vehicles_out_ids()
    available = []
    for snap in db.collection("vehicles").stream():
        d = snap.to_dict() or {}
        if d.get("active") is False:
            continue
        vid = (d.get("vehicle_id") or snap.id or "").strip().upper()
        if not vid or vid in out_ids:
            continue
        available.append({
            "vehicle_id": vid,
            "vehicle": (d.get("vehicle") or "").strip(),
            "description": (d.get("description") or "").strip(),
        })
    available.sort(key=lambda v: v.get("description") or v.get("vehicle_id") or "")
    return available


def _resolve_vehicle_pick(incoming: str, vehicle_ids: list):
    raw = (incoming or "").strip().upper()
    if not raw or not vehicle_ids:
        return None
    if raw in vehicle_ids:
        idx = vehicle_ids.index(raw)
    elif raw.isdigit():
        n = int(raw)
        if n < 1 or n > len(vehicle_ids):
            return None
        idx = n - 1
    else:
        low = incoming.strip().lower()
        match = [i for i, v in enumerate(vehicle_ids) if v.lower() == low]
        if not match:
            return None
        idx = match[0]
    vid = vehicle_ids[idx]
    snap = db.collection("vehicles").document(vid).get()
    if not snap.exists:
        return None
    d = snap.to_dict() or {}
    return {
        "company_vehicle_id": vid,
        "company_vehicle": (d.get("vehicle") or "").strip(),
        "company_vehicle_description": (d.get("description") or "").strip(),
    }


def _employee_name_for(sender: str, session: dict | None) -> str:
    if session and session.get("employee_name"):
        return _chat_name(session["employee_name"])
    user = db.collection("users").document(sender).get()
    if user.exists:
        return _chat_name(user.to_dict().get("name"))
    return "Employee"


def _go_back_to_main_menu(sender: str, session: dict | None = None) -> None:
    name = _employee_name_for(sender, session)
    _session_merge(sender, state=_SESSION_MENU_IDLE, employee_name=name)
    _send_main_menu(sender, name)


def _cancel_od_flow(sender: str, session: dict | None = None) -> None:
    _session_merge(sender, state=_SESSION_AWAITING_HI)
    _send_to(sender, "OD cancelled.\nSend Hi when you need the menu.")


def _build_od_summary(session: dict) -> str:
    reason = (session.get("od_reason") or "—").strip()
    uses_cv = session.get("uses_company_vehicle")
    lines = [
        "Please review your OD request:\n",
        "Request type: OD Request",
        f"Reason: {reason}",
    ]
    if uses_cv is True:
        desc = (session.get("company_vehicle_description") or "").strip()
        vid = (session.get("company_vehicle_id") or "").strip()
        vehicle_line = desc or vid or "—"
        lines.append(f"Company vehicle: Yes — {vehicle_line}")
    elif uses_cv is False:
        lines.append("Company vehicle: No")
    else:
        lines.append("Company vehicle: —")
    lines.append("\nSubmit | Cancel | Back")
    return "\n".join(lines)


def _send_od_confirm(sender: str, session: dict) -> None:
    body = _build_od_summary(session)
    _session_merge(sender, state="WAITING_OD_CONFIRM")
    try:
        send_reply_buttons(
            wa_id_to_phone(sender),
            body,
            [("SUBMIT", "Submit"), ("CANCEL", "Cancel"), ("BACK", "Back")],
            callback_data="od-confirm",
        )
    except Exception:
        logger.exception("OD confirm buttons failed")
        _send_to(sender, f"{body}\n\nReply SUBMIT, CANCEL, or BACK.")


def _show_od_confirm(sender: str, session: dict) -> None:
    snap = _session_ref(sender).get()
    data = {**(snap.to_dict() if snap.exists else {}), **session}
    _session_merge(
        sender,
        state="WAITING_OD_CONFIRM",
        od_reason=data.get("od_reason"),
        uses_company_vehicle=data.get("uses_company_vehicle"),
        company_vehicle_id=data.get("company_vehicle_id") or "",
        company_vehicle=data.get("company_vehicle") or "",
        company_vehicle_description=data.get("company_vehicle_description") or "",
        employee_name=data.get("employee_name"),
        vehicle_ids=data.get("vehicle_ids"),
    )
    fresh = _session_ref(sender).get()
    _send_od_confirm(sender, fresh.to_dict() if fresh.exists else data)


def _submit_od_from_session(sender: str, session: dict) -> None:
    reason = (session.get("od_reason") or "").strip()
    if not reason:
        _send_to(sender, "Missing OD reason. Send Hi to start again.")
        return
    _submit_od(
        sender,
        reason,
        uses_company_vehicle=bool(session.get("uses_company_vehicle")),
        company_vehicle_id=session.get("company_vehicle_id") or "",
        company_vehicle=session.get("company_vehicle") or "",
        company_vehicle_description=session.get("company_vehicle_description") or "",
    )


def _submit_od(
    sender: str,
    reason: str,
    *,
    uses_company_vehicle: bool = False,
    company_vehicle_id: str = "",
    company_vehicle: str = "",
    company_vehicle_description: str = "",
) -> None:
    if _find_open_od_for_employee(sender):
        db.collection("sessions").document(sender).delete()
        _send_to(sender, OD_ALREADY_PENDING_MSG)
        return

    user_doc = db.collection("users").document(sender).get()
    if not user_doc.exists:
        db.collection("sessions").document(sender).delete()
        _send_to(sender, "User not registered.\nPlease contact admin.")
        return

    ud = user_doc.to_dict()
    chain = _build_approval_chain(ud)
    if not chain:
        db.collection("sessions").document(sender).delete()
        _send_to(sender, "Approval chain not configured.\nPlease contact admin.")
        return

    ref = db.collection("requests").document()
    request_id = ref.id
    ref.set({
        "request_id": request_id,
        "requested_datetime": _utcnow(),
        "employee": sender,
        "employee_id": ud.get("employee_id") or "",
        "employee_name": ud.get("name") or "Employee",
        "department": ud.get("department") or "",
        "type": "OD",
        "reason": reason,
        "uses_company_vehicle": uses_company_vehicle,
        "company_vehicle_id": company_vehicle_id or "",
        "company_vehicle": company_vehicle or "",
        "company_vehicle_description": company_vehicle_description or "",
        "jmd": chain["jmd"],
        "jmd_route": chain["jmd_route"],
        "md": chain["md"],
        "manager_status": "N/A",
        "jmd_status": "PENDING",
        "md_status": "AWAITING_JMD",
    })
    logger.info("OD created %s jmd_route=%s", request_id, chain["jmd_route"])

    jmd_wa = chain["jmd"]
    jmd_ok = _send_approval_buttons(
        jmd_wa,
        employee_name=ud.get("name"),
        department=ud.get("department"),
        reason=reason,
        request_id=request_id,
    )
    if jmd_ok:
        _set_pending_approval(jmd_wa, request_id)

    db.collection("sessions").document(sender).delete()
    msg = "OD is Submitted."
    if uses_company_vehicle and company_vehicle_description:
        msg += f"\nVehicle: {company_vehicle_description}."
    if not jmd_ok:
        route = chain["jmd_route"]
        msg += (
            f"\n\nJMD ({route}) could not be notified on WhatsApp. "
            "Ask them to send Hi to this Alubee number once, then try again or contact admin."
        )
    _send_to(sender, msg)


def _normalize_od_reason_choice(incoming: str) -> str:
    choice = incoming.strip().upper().replace(" ", "_")
    if choice == "1":
        return "UNIT_I"
    if choice == "2":
        return "UNIT_II"
    if choice == "3":
        return "OTHER"
    if choice == "UNITII":
        return "UNIT_II"
    return choice


def _go_back_to_od_reason_pick(sender: str, session: dict | None = None) -> None:
    name = _employee_name_for(sender, session)
    _session_merge(sender, state="WAITING_OD_REASON_PICK", employee_name=name)
    _send_od_reason_buttons(sender)


def _go_back_to_company_vehicle(sender: str, reason: str, session: dict | None = None) -> None:
    name = _employee_name_for(sender, session)
    _session_merge(
        sender,
        state="WAITING_COMPANY_VEHICLE_YESNO",
        od_reason=reason,
        employee_name=name,
        uses_company_vehicle=None,
        company_vehicle_id="",
        company_vehicle="",
        company_vehicle_description="",
    )
    _send_company_vehicle_buttons(sender, reason)


def _go_back_from_confirm(sender: str, session: dict) -> None:
    reason = (session.get("od_reason") or "").strip()
    if session.get("uses_company_vehicle") and (
        session.get("company_vehicle_id") or session.get("vehicle_ids")
    ):
        ids = session.get("vehicle_ids") or []
        if session.get("company_vehicle_id") and session["company_vehicle_id"] not in ids:
            ids = list(ids) + [session["company_vehicle_id"]]
        vehicles = _fetch_vehicles()
        if not vehicles:
            _go_back_to_company_vehicle(sender, reason, session)
            return
        _session_merge(
            sender,
            state="WAITING_VEHICLE_PICK",
            od_reason=reason,
            vehicle_ids=[v["vehicle_id"] for v in vehicles],
            employee_name=session.get("employee_name"),
            uses_company_vehicle=True,
        )
        _send_dynamic_vehicle_list(sender, vehicles)
        return
    _go_back_to_company_vehicle(sender, reason, session)


def _prompt_od_reason_typing(sender: str, session: dict | None = None) -> None:
    name = _employee_name_for(sender, session)
    _session_merge(sender, state="WAITING_OD_REASON_TYPING", employee_name=name)
    try:
        send_reply_buttons(
            wa_id_to_phone(sender),
            "Please write OD reason:",
            [("BACK", "Back")],
            callback_data="od-reason-type",
        )
    except Exception:
        logger.exception("OD reason typing prompt failed")
        _send_to(sender, "Please write OD reason:")


def _prompt_company_vehicle(sender: str, reason: str, session: dict | None = None) -> None:
    name = _employee_name_for(sender, session)
    _session_merge(
        sender,
        state="WAITING_COMPANY_VEHICLE_YESNO",
        od_reason=reason,
        employee_name=name,
    )
    _send_company_vehicle_buttons(sender, reason)


def _locked_step_hint(session: dict) -> str:
    state = session.get("state")
    reason = (session.get("od_reason") or "").strip()
    if state == "WAITING_OD_REASON_PICK":
        return "Choose Unit I, Unit II, Other, or Back."
    if state == "WAITING_COMPANY_VEHICLE_YESNO":
        return f"Reason: {reason}. Tap YES, NO, or Back."
    if state == "WAITING_VEHICLE_PICK":
        return "Pick a vehicle from the list, or reply BACK."
    if state == "WAITING_OD_REASON_TYPING":
        return "Write your OD reason, or tap Back."
    if state == "WAITING_OD_CONFIRM":
        return "Tap Submit, Cancel, or Back to review or change your choices."
    return "Use the options for this step, or send Hi to start over."


def _handle_od_back(sender: str, session: dict) -> None:
    state = session.get("state")
    reason = (session.get("od_reason") or "").strip()

    if state == "WAITING_OD_REASON_PICK":
        _go_back_to_main_menu(sender, session)
        return
    if state == "WAITING_OD_REASON_TYPING":
        _go_back_to_od_reason_pick(sender, session)
        return
    if state == "WAITING_COMPANY_VEHICLE_YESNO":
        _go_back_to_od_reason_pick(sender, session)
        return
    if state == "WAITING_VEHICLE_PICK":
        _go_back_to_company_vehicle(sender, reason, session)
        return
    if state == "WAITING_OD_CONFIRM":
        _go_back_from_confirm(sender, session)
        return
    _go_back_to_main_menu(sender, session)


def _handle_od_session(sender: str, incoming: str, session: dict) -> None:
    state = session.get("state")
    choice = incoming.strip().upper().replace(" ", "_")
    reason = (session.get("od_reason") or "").strip()

    if choice == "BACK":
        _handle_od_back(sender, session)
        return

    if choice == "CANCEL":
        if state == "WAITING_OD_CONFIRM":
            _cancel_od_flow(sender, session)
        else:
            _send_to(sender, "You can cancel on the final review screen, or reply BACK step by step.")
        return

    if state == "WAITING_OD_CONFIRM":
        if choice == "SUBMIT":
            _submit_od_from_session(sender, session)
            return
        if choice not in _CONFIRM_CHOICES:
            _send_to(sender, _locked_step_hint(session))
        return

    if state == "WAITING_OD_REASON_PICK":
        choice = _normalize_od_reason_choice(incoming)
        if choice not in _OD_REASON_CHOICES:
            _send_to(sender, "Choose Unit I, Unit II, or Other — or send Hi to start over.")
            return
        if choice == "UNIT_I":
            _prompt_company_vehicle(sender, "Unit I")
        elif choice == "UNIT_II":
            _prompt_company_vehicle(sender, "Unit II")
        else:
            _prompt_od_reason_typing(sender, session)

    elif state == "WAITING_OD_REASON_TYPING":
        if choice in _OD_REASON_CHOICES or choice in _CONFIRM_CHOICES:
            _send_to(sender, _locked_step_hint(session))
            return
        reason_text = incoming.strip()
        if reason_text:
            _prompt_company_vehicle(sender, reason_text, session)
        else:
            _send_to(sender, "Please write OD reason, or tap Back.")

    elif state == "WAITING_COMPANY_VEHICLE_YESNO":
        if choice in _OD_REASON_CHOICES or choice in _CONFIRM_CHOICES:
            _send_to(sender, _locked_step_hint(session))
            return
        if choice not in _COMPANY_VEHICLE_CHOICES:
            _send_to(sender, _locked_step_hint(session))
            return
        if choice == "YES":
            vehicles = _fetch_vehicles()
            if not vehicles:
                db.collection("sessions").document(sender).delete()
                _send_to(
                    sender,
                    "No company vehicles available. Send Hi to try again.",
                )
            else:
                ids = [v["vehicle_id"] for v in vehicles]
                _session_merge(
                    sender,
                    state="WAITING_VEHICLE_PICK",
                    od_reason=reason,
                    vehicle_ids=ids,
                    uses_company_vehicle=True,
                    employee_name=session.get("employee_name"),
                )
                _send_dynamic_vehicle_list(sender, vehicles)
        else:
            _show_od_confirm(
                sender,
                {
                    **session,
                    "od_reason": reason,
                    "uses_company_vehicle": False,
                    "company_vehicle_id": "",
                    "company_vehicle": "",
                    "company_vehicle_description": "",
                },
            )

    elif state == "WAITING_VEHICLE_PICK":
        if choice in _OD_REASON_CHOICES or choice in _COMPANY_VEHICLE_CHOICES:
            _send_to(sender, _locked_step_hint(session))
            return
        ids = session.get("vehicle_ids") or []
        picked = _resolve_vehicle_pick(incoming, ids)
        if picked:
            _show_od_confirm(
                sender,
                {
                    **session,
                    "od_reason": reason,
                    "uses_company_vehicle": True,
                    "company_vehicle_id": picked["company_vehicle_id"],
                    "company_vehicle": picked["company_vehicle"],
                    "company_vehicle_description": picked["company_vehicle_description"],
                },
            )
        else:
            _send_to(
                sender,
                "Invalid selection. Pick from the list (number or ID), or reply BACK.",
            )


def _start_od(sender: str) -> None:
    user = db.collection("users").document(sender).get()
    name = "Employee"
    if user.exists:
        name = user.to_dict().get("name") or name
    _session_merge(
        sender,
        state="WAITING_OD_REASON_PICK",
        employee_name=name,
        form_type="OD_REQUEST",
    )
    _send_od_reason_buttons(sender)


def _extract_message(message_field) -> str:
    """Plain text, or list/button reply id from InteractiveListReply webhooks."""
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
            _session_merge(sender, state=_SESSION_MENU_IDLE, employee_name=name)
            _send_main_menu(sender, name)
        else:
            _session_ref(sender).delete()
            _send_to(sender, "User not registered.\nPlease contact admin.")
        return

    if _handle_approval_gate(sender, incoming):
        return

    session_doc = _session_ref(sender).get()
    session = session_doc.to_dict() if session_doc.exists else None
    state = (session or {}).get("state")

    if state == _SESSION_AWAITING_HI:
        _send_to(sender, "Send Hi to start.")
        return

    if state in _OD_SESSION_STATES:
        _handle_od_session(sender, incoming, session)
        return

    if incoming == "1" or incoming == "OD_REQUEST":
        if state == _SESSION_MENU_IDLE:
            _try_start_od(sender)
        else:
            _send_to(sender, "Send Hi to start.")
        return

    if incoming in ("2", "3", "4", "5") or incoming in _UNSUPPORTED_REQUEST_IDS:
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
        "runtime": "cloud_run" if _running_on_cloud_run() else "local",
        "api_key_set": bool(key),
        "whatsapp_session_hours": WHATSAPP_SESSION_HOURS,
        "approval_flow": "employee → JMD1|JMD2 → MD",
        "jmd_i": JMD_I_WHATSAPP_NUMBER,
        "jmd_ii": JMD_II_WHATSAPP_NUMBER,
        "md": MD_WHATSAPP_NUMBER,
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
