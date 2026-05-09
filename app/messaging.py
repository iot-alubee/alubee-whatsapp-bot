import re
import secrets
from twilio.rest import Client

from app.config import Settings


def twilio_client(settings: Settings) -> Client:
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


def whatsapp_address(raw: str) -> str:
    """Twilio expects whatsapp:+E164 for WhatsApp sends."""
    r = raw.strip()
    if not r:
        return r
    if r.lower().startswith("whatsapp:"):
        rest = r.split(":", 1)[1].strip()
        return f"whatsapp:{rest}" if rest.startswith("+") else f"whatsapp:+{rest}"
    if r.startswith("+"):
        return f"whatsapp:{r}"
    return f"whatsapp:+{r}"


def send_whatsapp(settings: Settings, to_whatsapp_from_style: str, body: str) -> None:
    """to_whatsapp_from_style may be +91... or whatsapp:+91..."""
    client = twilio_client(settings)
    client.messages.create(
        from_=whatsapp_address(settings.twilio_whatsapp_from),
        to=whatsapp_address(to_whatsapp_from_style),
        body=body,
    )


def new_approval_token() -> str:
    return secrets.token_hex(3).upper()


def parse_md_decision(body: str) -> tuple[str | None, str | None]:
    """
    Returns ('approve'|'deny', token) or (None, None).
    Accepts: APPROVE ABCDEF / DENY ABCDEF (case-insensitive)
    """
    text = body.strip().upper()
    m = re.match(r"^(APPROVE|DENY)\s+([A-F0-9]{6})$", text)
    if not m:
        return None, None
    action = m.group(1).lower()
    token = m.group(2)
    return ("approve" if action == "approve" else "deny"), token


def is_greeting(body: str) -> bool:
    b = body.strip().lower()
    return b in {"hi", "hello", "hey", "start", "menu", "hii"}
