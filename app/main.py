import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from twilio.request_validator import RequestValidator

from app.bigquery_client import fetch_employee, normalize_mobile_digits
from app.config import Settings
from app.messaging import (
    is_greeting,
    new_approval_token,
    parse_md_decision,
    send_whatsapp,
    whatsapp_address,
)
from app.sessions import clear_session, get_session, pending_pop, pending_put, upsert_session

load_dotenv()

app = FastAPI(title="Alubee OD WhatsApp")
settings = Settings()


def _ensure_gcp_credentials() -> None:
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return
    path = settings.google_application_credentials
    if not path:
        return
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    if p.is_file():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(p.resolve())


_ensure_gcp_credentials()


def _validate_twilio(request: Request, form: dict) -> None:
    if not settings.twilio_validate_webhook:
        return
    token = settings.twilio_auth_token
    if not token:
        return
    validator = RequestValidator(token)
    url = str(request.url)
    signature = request.headers.get("X-Twilio-Signature") or ""
    if not validator.validate(url, form, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


def _is_md(from_addr: str) -> bool:
    if not settings.md_whatsapp_to:
        return False
    return (
        from_addr.strip().lower()
        == whatsapp_address(settings.md_whatsapp_to).lower()
    )


async def _handle_md_inbound(from_addr: str, body: str) -> str | None:
    action, token = parse_md_decision(body)
    if not action or not token:
        return (
            "Send approval decisions as:\n"
            "APPROVE XXXXXX\n"
            "or\n"
            "DENY XXXXXX\n"
            "(XXXXXX is the 6-character code from each request.)"
        )

    payload = pending_pop(token)
    if not payload:
        return "This approval code is invalid or already processed."

    employee_to = payload["employee_from"]
    emp_name = payload["employee_name"]
    dept = payload["department"]
    reason = payload["reason"]

    if action == "approve":
        send_whatsapp(
            settings,
            employee_to,
            f"Your On Duty request is APPROVED.\n\nReason: {reason}",
        )
        return (
            f"Approved On Duty for {emp_name} ({dept}).\nReason: {reason}"
        )

    send_whatsapp(
        settings,
        employee_to,
        f"Your On Duty request was DENIED.\n\nReason submitted: {reason}",
    )
    return f"Denied On Duty for {emp_name} ({dept})."


def _ensure_employee(from_addr: str, body: str, raw_session: object | None):
    """Load employee from BigQuery on greeting; reset menu."""
    if not is_greeting(body):
        return None

    normalized = normalize_mobile_digits(from_addr)
    emp = fetch_employee(settings, normalized)
    if not emp:
        return (
            "Hi — your mobile number is not registered in our employee directory.\n"
            "Please contact HR or Admin."
        )

    upsert_session(
        from_addr,
        state="menu_main",
        employee_name=emp["employee_name"],
        department=emp["department"],
        od_reason_code=None,
        od_reason_text=None,
    )

    name = emp["employee_name"]
    dept = emp["department"]
    return (
        f"Hi {name} ({dept}).\n\n"
        "Choose an option (reply with the number):\n"
        "1 — On Duty Request\n"
        "2 — Vehicle Request\n"
    )


def _route_employee(from_addr: str, body: str, sess) -> str:
    text = body.strip()
    state = sess.state

    if state == "menu_main":
        if text == "1":
            upsert_session(from_addr, state="od_pick_visit")
            return (
                "On Duty — select reason (reply with number):\n"
                "1 — Visit U1\n"
                "2 — Visit U2\n"
                "3 — Other (you will type details next)"
            )
        if text == "2":
            return (
                "Vehicle Request is not configured yet.\n"
                "Send *hi* to open the menu again."
            )
        return "Reply *1* for On Duty or *2* for Vehicle."

    if state == "od_pick_visit":
        if text == "1":
            upsert_session(from_addr, state="idle", od_reason_code="U1", od_reason_text=None)
            refreshed = get_session(from_addr)
            return _submit_od(from_addr, refreshed)
        if text == "2":
            upsert_session(from_addr, state="idle", od_reason_code="U2", od_reason_text=None)
            refreshed = get_session(from_addr)
            return _submit_od(from_addr, refreshed)
        if text == "3":
            upsert_session(from_addr, state="od_other_reason")
            return "Please type your On Duty reason/details:"
        return "Reply *1*, *2*, or *3*."

    if state == "od_other_reason":
        reason_text = body.strip()
        if len(reason_text) < 3:
            return "Please enter a slightly longer reason (at least 3 characters)."
        upsert_session(
            from_addr,
            state="idle",
            od_reason_code="OTHER",
            od_reason_text=reason_text,
        )
        refreshed = get_session(from_addr)
        return _submit_od(from_addr, refreshed)

    return (
        "Send *hi* to see options.\n"
        "If you were entering text for Other, start again with *hi*."
    )


def _reason_display(sess) -> str:
    if sess.od_reason_code == "OTHER":
        return sess.od_reason_text or ""
    if sess.od_reason_code == "U1":
        return "Visit U1"
    if sess.od_reason_code == "U2":
        return "Visit U2"
    return sess.od_reason_code or ""


def _submit_od(from_addr: str, sess) -> str:
    reason = _reason_display(sess)
    if not reason:
        return "Something went wrong. Send hi to restart."

    token = new_approval_token()
    pending_put(
        token,
        {
            "employee_from": from_addr,
            "employee_name": sess.employee_name,
            "department": sess.department,
            "reason": reason,
        },
    )

    if settings.md_whatsapp_to:
        md_body = (
            f"On Duty approval needed\n\n"
            f"Employee: {sess.employee_name}\n"
            f"Department: {sess.department}\n"
            f"Reason: {reason}\n\n"
            f"Reply with:\n"
            f"APPROVE {token}\n"
            f"or\n"
            f"DENY {token}"
        )
        send_whatsapp(settings, settings.md_whatsapp_to, md_body)

    clear_session(from_addr)
    return (
        "Your On Duty request has been submitted for approval.\n\n"
        f"Reason: {reason}\n\n"
        "You will receive a WhatsApp update once MD decides."
    )


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/whatsapp/inbound")
async def whatsapp_inbound(request: Request):
    form = dict(await request.form())
    _validate_twilio(request, form)

    from_addr = form.get("From") or ""
    body = (form.get("Body") or "").strip()

    if not from_addr:
        return PlainTextResponse("", status_code=200)

    if _is_md(from_addr):
        reply = await _handle_md_inbound(from_addr, body)
        if reply:
            send_whatsapp(settings, from_addr, reply)
        return PlainTextResponse("", status_code=200)

    greet_reply = _ensure_employee(from_addr, body, get_session(from_addr))
    if greet_reply:
        send_whatsapp(settings, from_addr, greet_reply)
        return PlainTextResponse("", status_code=200)

    sess = get_session(from_addr)
    if not sess or not sess.employee_name:
        send_whatsapp(
            settings,
            from_addr,
            "Send *hi* to begin and load your profile.",
        )
        return PlainTextResponse("", status_code=200)

    reply = _route_employee(from_addr, body, sess)
    send_whatsapp(settings, from_addr, reply)
    return PlainTextResponse("", status_code=200)


# Convenience for Twilio Studio / debugging — optional
@app.get("/whatsapp/inbound")
async def whatsapp_inbound_get():
    raise HTTPException(status_code=405, detail="POST only")
