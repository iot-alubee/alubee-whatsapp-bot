"""
Interakt Public API — Text, InteractiveList (menu / vehicles), InteractiveButton (quick reply).
"""

from __future__ import annotations

import logging
import os
import ssl
import time
from pathlib import Path
from typing import Any

import certifi
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import SSLError as RequestsSSLError
from requests.exceptions import Timeout as RequestsTimeout
from urllib3.util.retry import Retry
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).resolve().parent
if not os.getenv("INTERAKT_API_KEY"):
    for name in (".env", ".env.example"):
        path = _APP_DIR / name
        if path.is_file():
            load_dotenv(path, override=True)
            break

INTERAKT_MESSAGE_URL = "https://api.interakt.ai/v1/public/message/"
INTERAKT_TRACK_USERS_URL = "https://api.interakt.ai/v1/public/track/users/"


def _api_key() -> str:
    return (os.getenv("INTERAKT_API_KEY") or "").strip()


def _headers() -> dict[str, str]:
    key = _api_key()
    if not key:
        raise ValueError("INTERAKT_API_KEY is not set")
    return {
        "Authorization": f"Basic {key}",
        "Content-Type": "application/json",
    }


def phone_to_10(phone: str) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())[-10:]


def wa_id_to_phone(wa_id: str) -> str:
    return phone_to_10((wa_id or "").replace("whatsapp:", ""))


def phone_to_wa_id(phone: str) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) == 10:
        digits = "91" + digits
    elif len(digits) > 10 and not digits.startswith("91"):
        digits = digits[-12:] if digits.startswith("91") else "91" + digits[-10:]
    return f"whatsapp:+{digits}"


_HTTP_RETRY_ATTEMPTS = 5
_HTTP_RETRY_BACKOFF_SEC = 1.0

_http_session: requests.Session | None = None


def _http_session_get() -> requests.Session:
    global _http_session
    if _http_session is not None:
        return _http_session
    session = requests.Session()
    session.verify = certifi.where()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.6,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(["POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    _http_session = session
    return session


def _is_transient_http_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (RequestsSSLError, RequestsConnectionError, RequestsTimeout, ssl.SSLEOFError),
    ):
        return True
    cause = getattr(exc, "__cause__", None)
    return isinstance(cause, ssl.SSLEOFError) if cause else False


def _http_post(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: int = 30,
    label: str = "Interakt",
) -> requests.Response:
    """POST with retries on transient TLS/network errors (common on Cloud Run)."""
    session = _http_session_get()
    last_err: Exception | None = None
    for attempt in range(1, _HTTP_RETRY_ATTEMPTS + 1):
        try:
            return session.post(
                url,
                json=payload,
                headers=_headers(),
                timeout=timeout,
            )
        except Exception as e:
            if not _is_transient_http_error(e):
                raise
            last_err = e
            logger.warning(
                "%s POST attempt %s/%s failed: %s",
                label,
                attempt,
                _HTTP_RETRY_ATTEMPTS,
                e,
            )
            if attempt < _HTTP_RETRY_ATTEMPTS:
                time.sleep(_HTTP_RETRY_BACKOFF_SEC * attempt)
    assert last_err is not None
    raise last_err


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    resp = _http_post(INTERAKT_MESSAGE_URL, payload, timeout=30, label="Interakt message")
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    if resp.status_code >= 400:
        logger.error("Interakt %s: %s", resp.status_code, data)
        raise RuntimeError(f"Interakt API {resp.status_code}: {data}")
    logger.info("Interakt sent type=%s status=%s", payload.get("type"), resp.status_code)
    return data


def ensure_customer(
    phone: str,
    *,
    name: str = "",
    user_id: str = "",
    required: bool = False,
) -> bool:
    """
    Register/update contact in Interakt before messaging (fixes
    'Customer is not available for the organization' for manager/MD).
    Returns True when track user succeeds (or is optional and fails softly).
    """
    phone10 = phone_to_10(phone)
    if not phone10:
        if required:
            logger.error("ensure_customer: invalid phone %r", phone)
        return False
    payload: dict[str, Any] = {
        "phoneNumber": phone10,
        "countryCode": "+91",
        "traits": {
            "name": (name or "Contact")[:256],
            "whatsapp_opted_in": True,
        },
        "tags": ["visitor_guest"],
    }
    if user_id:
        payload["userId"] = user_id[:256]
    try:
        resp = _http_post(
            INTERAKT_TRACK_USERS_URL,
            payload,
            timeout=15,
            label="Interakt track user",
        )
        if resp.status_code >= 400:
            logger.warning(
                "Interakt track user %s phone=%s: %s",
                resp.status_code,
                phone10,
                resp.text[:500],
            )
            return not required
        logger.info("Interakt track user ok phone=%s", phone10)
        return True
    except Exception:
        logger.exception("Interakt track user failed phone=%s", phone10)
        return not required


def send_template(
    phone: str,
    template_name: str,
    *,
    language_code: str = "en",
    body_values: list[str] | None = None,
    button_values: dict[str, list[str]] | None = None,
    callback_data: str = "",
    campaign_id: str = "",
    ensure_contact: bool = False,
    contact_name: str = "",
) -> dict[str, Any]:
    """
    Approved WhatsApp template — works without an active 24h session (business-initiated).
    Authentication templates also need buttonValues (same OTP in body + copy button).
    """
    if ensure_contact:
        ensure_customer(phone, name=contact_name or "Guest")
    template: dict[str, Any] = {
        "name": template_name.strip(),
        "languageCode": (language_code or "en").strip(),
    }
    if body_values:
        template["bodyValues"] = [str(v)[:1024] for v in body_values]
    if button_values:
        template["buttonValues"] = {
            k: [str(v)[:15] for v in vals]
            for k, vals in button_values.items()
        }
    payload: dict[str, Any] = {
        "countryCode": "+91",
        "phoneNumber": phone_to_10(phone),
        "type": "Template",
        "template": template,
    }
    if callback_data:
        payload["callbackData"] = callback_data[:512]
    if campaign_id:
        payload["campaignId"] = campaign_id[:256]
    return _post(payload)


def send_text(phone: str, text: str, *, callback_data: str = "") -> dict[str, Any]:
    """Text requires data.message as a plain string (per Interakt API)."""
    payload: dict[str, Any] = {
        "countryCode": "+91",
        "phoneNumber": phone_to_10(phone),
        "type": "Text",
        "data": {"message": text},
    }
    if callback_data:
        payload["callbackData"] = callback_data[:512]
    return _post(payload)


def send_interactive_list(
    phone: str,
    body_text: str,
    button_label: str,
    sections: list[dict[str, Any]],
    *,
    callback_data: str = "",
) -> dict[str, Any]:
    """Uses the nested data.message.type=list shape that works on your account."""
    payload: dict[str, Any] = {
        "countryCode": "+91",
        "phoneNumber": phone_to_10(phone),
        "type": "InteractiveList",
        "data": {
            "message": {
                "type": "list",
                "body": {"text": body_text},
                "action": {
                    "button": button_label[:20],
                    "sections": sections,
                },
            },
        },
    }
    if callback_data:
        payload["callbackData"] = callback_data[:512]
    return _post(payload)


def send_list_menu(
    phone: str,
    body_text: str,
    rows: list[dict[str, str]],
    *,
    button_label: str = "View Options",
    section_title: str = "Requests",
    callback_data: str = "",
) -> dict[str, Any]:
    """
    InteractiveList session message. ``rows`` are built at runtime (e.g. vehicles from DB).
    Each row: {"id": "...", "title": "...", "description": "..."} — max 10 rows per message.
    """
    if not rows:
        raise ValueError("send_list_menu requires at least one row")
    if len(rows) > 10:
        raise ValueError("WhatsApp allows max 10 rows per InteractiveList")
    sections = [{"title": section_title[:24], "rows": rows}]
    return send_interactive_list(
        phone,
        body_text,
        button_label,
        sections,
        callback_data=callback_data,
    )


def send_list_menu_paged(
    phone: str,
    body_text: str,
    all_rows: list[dict[str, str]],
    *,
    button_label: str = "View Options",
    section_title: str = "Options",
    callback_data: str = "",
) -> int:
    """Send one or more InteractiveList messages (10 rows each). Returns messages sent."""
    if not all_rows:
        return 0
    pages = [all_rows[i : i + 10] for i in range(0, len(all_rows), 10)]
    sent = 0
    for i, page_rows in enumerate(pages):
        body = body_text
        if len(pages) > 1:
            body = f"{body_text}\n\nPage {i + 1} of {len(pages)}:"
        send_list_menu(
            phone,
            body,
            page_rows,
            button_label=button_label,
            section_title=section_title if i == 0 else f"{section_title[:18]} {i + 1}",
            callback_data=f"{callback_data}-p{i + 1}" if callback_data else "",
        )
        sent += 1
    return sent


def _reply_button_defs(buttons: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """(id, label) pairs — max 3 buttons, label ≤20 chars (WhatsApp)."""
    out = []
    for btn_id, label in buttons[:3]:
        out.append({
            "type": "reply",
            "reply": {
                "id": str(btn_id)[:256],
                "title": str(label)[:20],
            },
        })
    return out


def send_interactive_buttons(
    phone: str,
    body_text: str,
    buttons: list[tuple[str, str]],
    *,
    callback_data: str = "",
    ensure_contact: bool = False,
    contact_name: str = "",
) -> dict[str, Any]:
    """
    Quick-reply buttons — type InteractiveButton, data.message.type=button.
    buttons: [(id, label), ...] e.g. (\"UNIT_I\", \"Unit I\"), (\"APPROVE\", \"Approve\")
    """
    if ensure_contact:
        ensure_customer(phone, name=contact_name)

    wa_buttons = _reply_button_defs(buttons)
    payload: dict[str, Any] = {
        "countryCode": "+91",
        "phoneNumber": phone_to_10(phone),
        "type": "InteractiveButton",
        "data": {
            "message": {
                "type": "button",
                "body": {"text": body_text},
                "action": {"buttons": wa_buttons},
            },
        },
    }
    if callback_data:
        payload["callbackData"] = callback_data[:512]
    return _post(payload)


def send_reply_buttons(
    phone: str,
    body_text: str,
    buttons: list[tuple[str, str]],
    *,
    callback_data: str = "",
    ensure_contact: bool = False,
    contact_name: str = "",
) -> dict[str, Any]:
    return send_interactive_buttons(
        phone,
        body_text,
        buttons,
        callback_data=callback_data,
        ensure_contact=ensure_contact,
        contact_name=contact_name,
    )


def _template_body_values_from_spec(spec: str, values: dict[str, str]) -> list[str]:
    """Build bodyValues order from env e.g. name,otp,organization."""
    keys = [k.strip().lower() for k in (spec or "name,otp,organization").split(",") if k.strip()]
    out: list[str] = []
    for key in keys:
        out.append(str(values.get(key, ""))[:1024])
    return out


def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.getenv(name) or ("true" if default else "false")).strip().lower()
    return raw in ("1", "true", "yes", "on")


def send_flow_template(
    phone: str,
    template_name: str,
    *,
    language_code: str = "en",
    body_values: list[str] | None = None,
    callback_data: str = "",
    flow_token: str = "",
    flow_action_data: dict | None = None,
    ensure_contact: bool = False,
    contact_name: str = "",
) -> dict[str, Any]:
    """
    Send an approved WhatsApp Flow template (Interakt: is_flow_template=true).
    Works outside 24h session when template is utility/marketing approved.
    """
    if ensure_contact:
        ensure_customer(phone, name=contact_name or "Employee")

    template: dict[str, Any] = {
        "name": template_name.strip(),
        "languageCode": (language_code or "en").strip(),
        "is_flow_template": True,
    }
    if body_values:
        template["bodyValues"] = [str(v)[:1024] for v in body_values]

    token = (flow_token or "").strip()
    action = flow_action_data if isinstance(flow_action_data, dict) else {}
    if token or action:
        template["buttonPayload"] = {"0": ["flow_token"], "1": ["flow_action_data"]}
        template["buttonValues"] = {
            "0": [token[:256]],
            "1": [json.dumps(action, ensure_ascii=True)[:4096]],
        }
    else:
        template["buttonPayload"] = {"0": ["flow_token"], "1": ["flow_action_data"]}
        template["buttonValues"] = {"0": [""], "1": [""]}

    payload: dict[str, Any] = {
        "countryCode": "+91",
        "phoneNumber": phone_to_10(phone),
        "type": "Template",
        "template": template,
    }
    if callback_data:
        payload["callbackData"] = callback_data[:512]
    return _post(payload)


def send_visitor_flow_form(
    phone: str,
    *,
    employee_name: str = "",
    body_values: list[str] | None = None,
) -> bool:
    """Send visitor WhatsApp Form template (env VISITOR_FLOW_TEMPLATE_NAME)."""
    template_name = (os.getenv("VISITOR_FLOW_TEMPLATE_NAME") or "").strip()
    if not template_name:
        logger.warning("VISITOR_FLOW_TEMPLATE_NAME not set — cannot send visitor form")
        return False
    lang = (os.getenv("VISITOR_FLOW_TEMPLATE_LANGUAGE_CODE") or "en").strip()
    if body_values is None:
        spec = (os.getenv("VISITOR_FLOW_TEMPLATE_BODY_FIELDS") or "name").strip()
        keys = [k.strip() for k in spec.split(",") if k.strip()]
        vals = {"name": (employee_name or "Employee")[:50]}
        body_values = [str(vals.get(k, ""))[:1024] for k in keys]
    try:
        send_flow_template(
            phone,
            template_name,
            language_code=lang,
            body_values=body_values,
            callback_data="visitor-flow",
            ensure_contact=True,
            contact_name=(employee_name or "Employee")[:50],
        )
        logger.info("visitor flow template sent phone=%s template=%s", phone_to_10(phone), template_name)
        return True
    except Exception:
        logger.exception("visitor flow template failed phone=%s", phone_to_10(phone))
        return False


def send_guest_visit_otp(
    phone: str,
    *,
    guest_name: str,
    otp: str,
    organization: str,
) -> bool:
    """
    Send visit OTP to a guest who may never have messaged Alubee (no session).
    Default template: visitor_pass_code (Authentication — body + copy button).
    """
    template_name = (os.getenv("VISITOR_OTP_TEMPLATE_NAME") or "visitor_pass_code").strip()
    if not template_name:
        logger.error("VISITOR_OTP_TEMPLATE_NAME is empty — cannot WhatsApp guest")
        return False

    phone10 = phone_to_10(phone)
    if not phone10:
        logger.error("guest visit OTP: invalid phone %r", phone)
        return False

    if not ensure_customer(
        phone10,
        name=(guest_name or "Guest")[:50],
        user_id=f"visitor_{phone10}",
        required=True,
    ):
        logger.error("guest visit OTP: could not register guest phone=%s", phone10)
        return False

    lang = (os.getenv("VISITOR_OTP_TEMPLATE_LANGUAGE_CODE") or "en").strip()
    body_spec = (os.getenv("VISITOR_OTP_TEMPLATE_BODY_FIELDS") or "otp").strip()
    otp_code = str(otp or "").strip()[:15]
    if not otp_code:
        logger.error("guest visit OTP: empty otp")
        return False

    # Authentication templates: same OTP in bodyValues and buttonValues (Interakt docs).
    body_values = [otp_code]
    if body_spec and body_spec.strip().lower() != "otp":
        body_values = _template_body_values_from_spec(
            body_spec,
            {
                "name": (guest_name or "Guest")[:50],
                "otp": otp_code,
                "organization": (organization or "Alubee")[:200],
            },
        )

    button_values = None
    if _env_flag("VISITOR_OTP_TEMPLATE_AUTH_BUTTON", default=True):
        button_values = {"0": [otp_code]}

    def _send(*, with_button: bool) -> dict[str, Any]:
        return send_template(
            phone10,
            template_name,
            language_code=lang,
            body_values=body_values,
            button_values=button_values if with_button else None,
            callback_data="visitor-otp",
            ensure_contact=False,
            contact_name=(guest_name or "Guest")[:50],
        )

    try:
        data = _send(with_button=bool(button_values))
        logger.info(
            "guest visit OTP template sent phone=%s template=%s response=%s",
            phone10,
            template_name,
            str(data)[:200],
        )
        return True
    except Exception:
        logger.exception(
            "guest visit OTP template failed phone=%s template=%s body=%s button=%s",
            phone10,
            template_name,
            body_values,
            button_values,
        )
        if button_values:
            try:
                data = _send(with_button=False)
                logger.info(
                    "guest visit OTP sent without auth button phone=%s response=%s",
                    phone10,
                    str(data)[:200],
                )
                return True
            except Exception:
                logger.exception(
                    "guest visit OTP retry without button failed phone=%s", phone10
                )
        return False
