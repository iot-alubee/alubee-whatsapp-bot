from contextlib import contextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response

from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client

import firebase_admin
from firebase_admin import credentials, firestore

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent

logger = logging.getLogger(__name__)

# =========================================================
# FIREBASE SETUP (local JSON / env JSON / ADC on Cloud Run)
# =========================================================

_CLOUD_RUN_STRIP_CRED_ENV = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "FIREBASE_CREDENTIALS_PATH",
    "FIREBASE_CREDENTIALS_JSON",
)


def _running_on_cloud_run() -> bool:
    return bool(os.environ.get("K_SERVICE"))


if not os.environ.get("K_SERVICE"):
    from dotenv import load_dotenv

    load_dotenv(_APP_DIR / ".env")


@contextmanager
def _cloud_run_metadata_credentials_env():
    saved = {k: os.environ.pop(k) for k in _CLOUD_RUN_STRIP_CRED_ENV if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def _init_firebase() -> None:
    project_id = (
        os.getenv("FIREBASE_PROJECT_ID", "whatsapp-approval-system") or ""
    ).strip()
    init_options = {"projectId": project_id} if project_id else None

    def _try_adc():
        cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred, init_options)

    try:
        firebase_admin.get_app()
        return
    except ValueError:
        pass

    if _running_on_cloud_run():
        if any(os.environ.get(k) for k in _CLOUD_RUN_STRIP_CRED_ENV):
            logger.warning(
                "Cloud Run: ignoring FIREBASE_CREDENTIALS_JSON / "
                "GOOGLE_APPLICATION_CREDENTIALS; use service account IAM instead."
            )
        with _cloud_run_metadata_credentials_env():
            _try_adc()
        return

    json_raw = (os.getenv("FIREBASE_CREDENTIALS_JSON") or "").strip()
    if json_raw:
        cred = credentials.Certificate(json.loads(json_raw))
        firebase_admin.initialize_app(cred, init_options)
        return

    cred_path = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if not cred_path:
        cred_path = "firebase-adminsdk.json"
    if cred_path and os.path.isfile(cred_path):
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred, init_options)
        return

    _try_adc()


_init_firebase()
db = firestore.client()

# =========================================================
# TWILIO CONFIG (local: .env | Cloud Run: service env vars)
# =========================================================

TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_NUMBER = (
    os.getenv("TWILIO_WHATSAPP_NUMBER") or "whatsapp:+14155238886"
).strip()

if _running_on_cloud_run() and (
    not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_WHATSAPP_NUMBER
):
    raise RuntimeError(
        "Cloud Run requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and "
        "TWILIO_WHATSAPP_NUMBER environment variables."
    )

# List picker: request_form_selector (body uses {{1}} = employee name)
CONTENT_SID_LIST_PICKER_MENU = "HX36b7b338c394e71733798f3bf1745150"

# OD reason: quick reply (UNIT_I, UNIT_II, OTHER — if OTHER, user types reason next)
CONTENT_SID_OD_REASON = "HX6fe91feaaea6c4199a0bf9bc5d3d7e19"

# Company vehicle yes/no (list items YES, NO)
CONTENT_SID_COMPANY_VEHICLE = "HX0f96bda3d09d5727cd1d22b665207d27"

# OD approval quick reply (manager + MD): {{1}} name, {{3}} reason, {{4}} dept; buttons Approve / DENY
CONTENT_SID_OD_APPROVAL = "HX254119eaa6782170b0000d9390b3000f"

# MD WhatsApp id (must match load_users MD_MOBILE; override with env MD_WHATSAPP_NUMBER)
MD_WHATSAPP_NUMBER = os.getenv(
    "MD_WHATSAPP_NUMBER",
    "whatsapp:+917538866308",
).strip()

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


REQUEST_CANNOT_BE_RAISED_MSG = (
    "This request cannot be raised now. Thanks!"
)

# Button / list item IDs for request types that are not implemented yet
_UNSUPPORTED_REQUEST_IDS = frozenset({
    "VEHICLE_REQUEST",
    "LEAVE_REQUEST",
    "PERMISSION_REQUEST",
    "VISITOR_REQUEST",
})

# OD flow session states (must be handled before menu shortcuts "1"–"5")
_OD_SESSION_STATES = frozenset({
    "WAITING_OD_REASON_PICK",
    "WAITING_OD_REASON_TYPING",
    "WAITING_COMPANY_VEHICLE_YESNO",
    "WAITING_VEHICLE_PICK",
})


def _utcnow():
    return datetime.now(timezone.utc)


def _chat_name(name) -> str:
    """Employee name for WhatsApp messages (e.g. AJAY SENTHILKUMAR -> Ajay Senthilkumar)."""
    raw = str(name or "").strip()
    if not raw:
        return "Employee"
    return raw.title()


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


def _content_var(value) -> str:
    """Twilio Content variables: single-line strings only (error 21656 if newlines)."""
    s = str(value or "").strip()
    s = " ".join(s.split())
    return s[:256] if s else "Employee"


def _content_variables_json(**kwargs) -> str:
    return json.dumps({str(k): _content_var(v) for k, v in kwargs.items()})


def _list_picker_menu_variables(employee_name) -> str:
    return _content_variables_json(
        **{"1": _chat_name(employee_name) or "Employee"}
    )


def _parse_incoming_choice(form_data) -> str:
    """Prefer list/button IDs from Twilio templates; fall back to Body or numbers."""
    list_id = (form_data.get("ListId") or "").strip()
    if list_id:
        return list_id
    button_payload = (form_data.get("ButtonPayload") or "").strip()
    if button_payload:
        return button_payload
    body = (form_data.get("Body") or "").strip()
    if not body:
        return ""
    key = body.lower()
    menu_titles = {
        "od request": "OD_REQUEST",
        "vehicle request": "VEHICLE_REQUEST",
        "leave request": "LEAVE_REQUEST",
        "permission request": "PERMISSION_REQUEST",
        "visitor request": "VISITOR_REQUEST",
    }
    return menu_titles.get(key, body)


def _same_whatsapp(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a.strip().lower() == b.strip().lower()


def _approval_reason_text(request_data: dict, reason: str = "") -> str:
    """Reason line for approval template {{3}} (includes company vehicle if set)."""
    base = (reason or (request_data or {}).get("reason") or "").strip()
    if (request_data or {}).get("uses_company_vehicle") and (
        request_data or {}
    ).get("company_vehicle_description"):
        return f"{base} (Vehicle: {request_data['company_vehicle_description']})"
    return base


def _od_content_variables(
    *,
    employee_name,
    reason,
    request_id,
    department,
) -> str:
    """Twilio od_approval_template: {{1}} employee, {{3}} reason, {{4}} department."""
    return _content_variables_json(
        **{
            "1": _chat_name(employee_name),
            "3": reason or "",
            "4": department or "—",
        }
    )


def _set_pending_approval_session(recipient: str, request_id: str) -> None:
    """Link Approve/DENY taps (fixed button IDs) to this request for manager/MD."""
    db.collection("sessions").document(recipient).set({
        "state": "WAITING_APPROVAL_ACTION",
        "approval_request_id": request_id,
    })


def _resolve_approval_request_id(incoming_msg: str, approver: str):
    """Return (is_approve, request_id) or (None, None) if not an approval message."""
    raw = (incoming_msg or "").strip()
    um = raw.upper()
    if um.startswith("APPROVE_"):
        rid = raw.split("_", 1)[1].strip()
        return (True, rid) if rid else (None, None)
    if um.startswith("DENY_"):
        rid = raw.split("_", 1)[1].strip()
        return (False, rid) if rid else (None, None)
    if raw in ("Approve", "APPROVE") or um == "APPROVE":
        snap = db.collection("sessions").document(approver).get()
        if snap.exists:
            data = snap.to_dict() or {}
            if data.get("state") == "WAITING_APPROVAL_ACTION":
                rid = (data.get("approval_request_id") or "").strip()
                if rid:
                    return True, rid
    if raw in ("Deny", "DENY") or um == "DENY":
        snap = db.collection("sessions").document(approver).get()
        if snap.exists:
            data = snap.to_dict() or {}
            if data.get("state") == "WAITING_APPROVAL_ACTION":
                rid = (data.get("approval_request_id") or "").strip()
                if rid:
                    return False, rid
    return None, None


def _handle_approval_gate(sender: str, incoming_msg: str, response) -> bool:
    """Manager/MD Approve or Deny. Returns True if this message was handled."""
    approval = _resolve_approval_request_id(incoming_msg, sender)
    if approval[0] is None:
        return False

    is_approve, request_id = approval
    request_ref = db.collection("requests").document(request_id)
    snap = request_ref.get()

    if not snap.exists:
        print("Approve/deny: request not found", request_id)
        return True

    rd = snap.to_dict()
    mgr_pending = rd.get("manager_status") == "PENDING"
    md_waiting = (
        rd.get("manager_status") == "APPROVED"
        and rd.get("md_status") == "PENDING"
    )
    handled = False

    if (
        md_waiting
        and MD_WHATSAPP_NUMBER
        and _same_whatsapp(sender, MD_WHATSAPP_NUMBER)
    ):
        if is_approve:
            request_ref.update({
                "md_status": "APPROVED",
                "approved_datetime": _utcnow(),
            })
            client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=rd["employee"],
                body="Your OD has been Approved.",
            )
        else:
            request_ref.update({"md_status": "DENIED"})
            client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=rd["employee"],
                body="Your OD request was not approved.",
            )
        handled = True

    elif mgr_pending and _same_whatsapp(sender, rd.get("manager")):
        if is_approve:
            if not MD_WHATSAPP_NUMBER:
                print(
                    "Manager approve: MD_WHATSAPP_NUMBER not set; "
                    "cannot complete flow",
                    request_id,
                )
            else:
                request_ref.update({
                    "manager_status": "APPROVED",
                    "md_status": "PENDING",
                })
                _notify_md_for_request(request_id, rd)
        else:
            request_ref.update({
                "manager_status": "DENIED",
                "md_status": "N/A",
            })
            client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=rd["employee"],
                body="Your OD request was not approved.",
            )
        handled = True

    if handled:
        db.collection("sessions").document(sender).delete()
    else:
        print(
            "Approve/deny: unauthorized or wrong state",
            request_id,
        )
    return True


def _vehicles_currently_out_ids():
    """Vehicle IDs that are out (Security OUT recorded, IN not yet)."""
    out_ids = set()
    for snap in db.collection("requests").stream():
        d = snap.to_dict() or {}
        if (d.get("type") or "").strip().upper() != "OD":
            continue
        vid = (d.get("company_vehicle_id") or "").strip().upper()
        if not vid:
            continue
        if d.get("security_out_at") and not d.get("security_in_at"):
            out_ids.add(vid)
    return out_ids


def _fetch_available_vehicles():
    """Active vehicles from Firestore, excluding those currently out."""
    out_ids = _vehicles_currently_out_ids()
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
            "make": (d.get("make") or "").strip(),
            "description": (d.get("description") or "").strip(),
        })
    available.sort(key=lambda v: (v.get("description") or v.get("vehicle_id") or ""))
    return available


def _send_company_vehicle_yes_no(sender: str) -> bool:
    """Send Company Vehicle? template. Returns True if sent OK."""
    try:
        tw_msg = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=sender,
            content_sid=CONTENT_SID_COMPANY_VEHICLE,
        )
        print(
            "Company vehicle template:",
            "sid=", tw_msg.sid,
            "status=", tw_msg.status,
            "error_code=", getattr(tw_msg, "error_code", None),
            "template=", CONTENT_SID_COMPANY_VEHICLE,
        )
        return not getattr(tw_msg, "error_code", None)
    except Exception as e:
        print(
            "Company vehicle template send failed:",
            repr(e),
            "template=", CONTENT_SID_COMPANY_VEHICLE,
        )
        return False


def _prompt_company_vehicle(sender: str, reason: str, response: MessagingResponse) -> None:
    """After OD reason: ask company vehicle yes/no."""
    db.collection("sessions").document(sender).set({
        "state": "WAITING_COMPANY_VEHICLE_YESNO",
        "od_reason": reason,
    })
    if _send_company_vehicle_yes_no(sender):
        return
    response.message().body(
        "Company vehicle?\nReply YES or NO."
    )


def _send_vehicle_pick_list(sender: str, vehicles: list) -> None:
    """Send available vehicles; user replies with list number or vehicle_id."""
    lines = ["Select company vehicle (reply with the number or vehicle ID):\n"]
    for i, v in enumerate(vehicles, start=1):
        desc = v.get("description") or v.get("vehicle_id")
        lines.append(f"{i}. {desc}")
    client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        to=sender,
        body="\n".join(lines),
    )


def _resolve_vehicle_pick(incoming: str, vehicle_ids: list):
    """Map user reply to vehicle dict fields or None if invalid."""
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
        return None
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


def _notify_md_for_request(request_id: str, request_data: dict):
    """After manager approval, notify MD using the same Content template as the manager."""
    if not MD_WHATSAPP_NUMBER:
        print("MD notify skipped: MD_WHATSAPP_NUMBER not set", request_id)
        return
    try:
        tw_msg = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=MD_WHATSAPP_NUMBER,
            content_sid=CONTENT_SID_OD_APPROVAL,
            content_variables=_od_content_variables(
                employee_name=request_data.get("employee_name"),
                reason=_approval_reason_text(request_data),
                request_id=request_id,
                department=request_data.get("department"),
            ),
        )
        _set_pending_approval_session(MD_WHATSAPP_NUMBER, request_id)
        print(
            "MD approval request sent:",
            "to=", MD_WHATSAPP_NUMBER,
            "request_id=", request_id,
            "sid=", tw_msg.sid,
            "status=", tw_msg.status,
            "error_code=", getattr(tw_msg, "error_code", None),
        )
    except Exception as e:
        print("MD notify failed:", request_id, repr(e))


def _submit_od_request(
    sender: str,
    reason: str,
    response: MessagingResponse,
    *,
    uses_company_vehicle: bool = False,
    company_vehicle_id: str = "",
    company_vehicle: str = "",
    company_vehicle_description: str = "",
) -> None:
    """Create OD request, notify manager, clear session; or set TwiML error on failure."""
    user_doc = db.collection("users").document(sender).get()

    if not user_doc.exists:
        db.collection("sessions").document(sender).delete()
        response.message().body(
            "User not registered.\nPlease contact admin."
        )
        return

    user_data = user_doc.to_dict()
    employee_name = user_data.get("name")
    manager_number = (user_data.get("manager") or "").strip()
    employee_id = user_data.get("employee_id", "")
    department = user_data.get("department", "")

    if not manager_number:
        db.collection("sessions").document(sender).delete()
        response.message().body(
            "No manager is set on your profile.\nPlease contact admin."
        )
        return

    doc_ref = db.collection("requests").document()
    request_id = doc_ref.id

    doc_ref.set({
        "request_id": request_id,
        "requested_datetime": _utcnow(),
        "employee": sender,
        "employee_id": employee_id or "",
        "employee_name": employee_name or "Employee",
        "department": department or "",
        "type": "OD",
        "reason": reason,
        "uses_company_vehicle": uses_company_vehicle,
        "company_vehicle_id": company_vehicle_id or "",
        "company_vehicle": company_vehicle or "",
        "company_vehicle_description": company_vehicle_description or "",
        "manager": manager_number,
        "manager_status": "PENDING",
        "md_status": "AWAITING_MANAGER",
    })

    print("Request ID:", request_id)

    approval_payload = {
        "employee_name": employee_name,
        "department": department,
        "uses_company_vehicle": uses_company_vehicle,
        "company_vehicle_description": company_vehicle_description,
        "reason": reason,
    }
    client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        to=manager_number,
        content_sid=CONTENT_SID_OD_APPROVAL,
        content_variables=_od_content_variables(
            employee_name=employee_name,
            reason=_approval_reason_text(approval_payload, reason),
            request_id=request_id,
            department=department,
        ),
    )
    _set_pending_approval_session(manager_number, request_id)

    db.collection("sessions").document(sender).delete()
    msg = "OD is Submitted."
    if uses_company_vehicle and company_vehicle_description:
        msg += f"\nVehicle: {company_vehicle_description}."
    response.message().body(msg)


def _handle_od_session(sender, incoming_msg, session_data, response):
    """Continue in-progress OD flow. Caller must only invoke for ``_OD_SESSION_STATES``."""
    state = session_data.get("state")

    if state == "WAITING_OD_REASON_PICK":

        choice = incoming_msg.strip().upper().replace(" ", "_")

        if choice == "UNIT_I":
            _prompt_company_vehicle(sender, "Unit I", response)
        elif choice in ("UNIT_II", "UNITII"):
            _prompt_company_vehicle(sender, "Unit II", response)
        elif choice == "OTHER":
            db.collection("sessions").document(sender).set({
                "state": "WAITING_OD_REASON_TYPING",
            })
            response.message().body(
                "Please type your OD reason."
            )
        else:
            response.message().body(
                "Please tap Unit I, Unit II, or Other on the message above, "
                "or send Hi to start over."
            )

    elif state == "WAITING_OD_REASON_TYPING":

        reason = incoming_msg.strip()
        if not reason:
            response.message().body("Please type your OD reason.")
        else:
            _prompt_company_vehicle(sender, reason, response)

    elif state == "WAITING_COMPANY_VEHICLE_YESNO":

        choice = incoming_msg.strip().upper()
        reason = (session_data.get("od_reason") or "").strip()

        if choice == "YES":
            vehicles = _fetch_available_vehicles()
            if not vehicles:
                db.collection("sessions").document(sender).delete()
                response.message().body(
                    "No company vehicles are available right now. "
                    "Your OD was not submitted. Send Hi to try again."
                )
            else:
                vehicle_ids = [v["vehicle_id"] for v in vehicles]
                db.collection("sessions").document(sender).set({
                    "state": "WAITING_VEHICLE_PICK",
                    "od_reason": reason,
                    "vehicle_ids": vehicle_ids,
                })
                _send_vehicle_pick_list(sender, vehicles)
        elif choice == "NO":
            _submit_od_request(
                sender,
                reason,
                response,
                uses_company_vehicle=False,
            )
        else:
            response.message().body(
                "Please tap YES or NO on the Company Vehicle message, "
                "or send Hi to start over."
            )

    elif state == "WAITING_VEHICLE_PICK":

        reason = (session_data.get("od_reason") or "").strip()
        vehicle_ids = session_data.get("vehicle_ids") or []
        picked = _resolve_vehicle_pick(incoming_msg, vehicle_ids)
        if not picked:
            response.message().body(
                "Invalid selection. Reply with the number or vehicle ID from the list."
            )
        else:
            _submit_od_request(
                sender,
                reason,
                response,
                uses_company_vehicle=True,
                company_vehicle_id=picked["company_vehicle_id"],
                company_vehicle=picked["company_vehicle"],
                company_vehicle_description=picked["company_vehicle_description"],
            )


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI()


@app.get("/")
def root():
    return {"service": "alubee-whatsapp-bot", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}


# =========================================================
# WEBHOOK (Twilio must POST here; "/" also accepted if URL omits /webhook)
# =========================================================

@app.api_route("/webhook", methods=["POST"])
@app.api_route("/", methods=["POST"])
async def whatsapp_webhook(request: Request):

    form_data = await request.form()
    print("Body:", form_data.get("Body"))
    print("ButtonPayload:", form_data.get("ButtonPayload"))
    print("ListId:", form_data.get("ListId"))

    incoming_msg = _parse_incoming_choice(form_data)
    sender = form_data.get("From", "")

    print("===================================")
    print("Message :", incoming_msg)
    print("Sender  :", sender)
    print("===================================")

    response = MessagingResponse()

    # =====================================================
    # START / HI
    # =====================================================

    if incoming_msg.lower() == "hi":

        # Abandon any in-progress OD / vehicle flow and return to main menu
        db.collection("sessions").document(sender).delete()

        # fetch user details
        user_doc = db.collection("users").document(sender).get()

        if user_doc.exists:

            user_data = user_doc.to_dict()

            employee_name = user_data.get("name", "Employee")

            menu_vars = _list_picker_menu_variables(employee_name)
            try:
                tw_msg = client.messages.create(
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=sender,
                    content_sid=CONTENT_SID_LIST_PICKER_MENU,
                    content_variables=menu_vars,
                )
                print(
                    "List picker menu:",
                    "sid=", tw_msg.sid,
                    "status=", tw_msg.status,
                    "error_code=", getattr(tw_msg, "error_code", None),
                    "content_variables=", menu_vars,
                )
                if getattr(tw_msg, "error_code", None):
                    response.message().body(_numbered_request_menu(employee_name))
            except Exception as e:
                print(
                    "List picker menu send failed:",
                    repr(e),
                    "template=", CONTENT_SID_LIST_PICKER_MENU,
                    "content_variables=", menu_vars,
                )
                response.message().body(_numbered_request_menu(employee_name))

        else:

            response.message().body(
                "User not registered.\n"
                "Please contact admin."
            )

    else:

        session_doc = db.collection("sessions").document(sender).get()
        session_data = session_doc.to_dict() if session_doc.exists else None
        state = (session_data or {}).get("state")

        # Before "session exists" check — approver has WAITING_APPROVAL_ACTION session
        if _handle_approval_gate(sender, incoming_msg, response):
            pass

        elif state in _OD_SESSION_STATES:
            # Before menu shortcuts: vehicle pick uses "1"–"13", not main menu
            _handle_od_session(sender, incoming_msg, session_data, response)

        elif incoming_msg == "1" or incoming_msg.upper() == "OD_REQUEST":

            db.collection("sessions").document(sender).set({
                "state": "WAITING_OD_REASON_PICK",
            })

            try:
                tw_msg = client.messages.create(
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=sender,
                    content_sid=CONTENT_SID_OD_REASON,
                )
                print(
                    "OD reason template:",
                    "sid=", tw_msg.sid,
                    "status=", tw_msg.status,
                    "error_code=", getattr(tw_msg, "error_code", None),
                    "template=", CONTENT_SID_OD_REASON,
                )
                if getattr(tw_msg, "error_code", None):
                    raise RuntimeError("Twilio returned error_code on OD reason template")
            except Exception as e:
                print(
                    "OD reason template send failed:",
                    repr(e),
                    "template=", CONTENT_SID_OD_REASON,
                )
                db.collection("sessions").document(sender).set({
                    "state": "WAITING_OD_REASON_TYPING",
                })
                response.message().body(
                    "Please type your OD reason (reason template could not be sent)."
                )

        elif (
            incoming_msg in ("2", "3", "4", "5")
            or incoming_msg.upper() in _UNSUPPORTED_REQUEST_IDS
        ):

            response.message().body(REQUEST_CANNOT_BE_RAISED_MSG)

        elif session_doc.exists:

            response.message().body("Invalid session state")

        else:

            response.message().body(
                "Send 'Hi' to start."
            )

    return Response(
        content=str(response),
        media_type="application/xml"
    )