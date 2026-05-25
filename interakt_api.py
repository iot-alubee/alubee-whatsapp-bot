"""
Interakt Public API — Text, InteractiveList (menu / vehicles), InteractiveButton (quick reply).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).resolve().parent
if not os.getenv("INTERAKT_API_KEY") and not os.getenv("K_SERVICE"):
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


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(
        INTERAKT_MESSAGE_URL,
        json=payload,
        headers=_headers(),
        timeout=30,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    if resp.status_code >= 400:
        logger.error("Interakt %s: %s", resp.status_code, data)
        raise RuntimeError(f"Interakt API {resp.status_code}: {data}")
    logger.info("Interakt sent type=%s status=%s", payload.get("type"), resp.status_code)
    return data


def ensure_customer(phone: str, *, name: str = "", user_id: str = "") -> None:
    """
    Register/update contact in Interakt before messaging (fixes
    'Customer is not available for the organization' for manager/MD).
    """
    payload: dict[str, Any] = {
        "phoneNumber": phone_to_10(phone),
        "countryCode": "+91",
        "traits": {
            "name": (name or "Contact")[:256],
            "whatsapp_opted_in": True,
        },
    }
    if user_id:
        payload["userId"] = user_id[:256]
    try:
        resp = requests.post(
            INTERAKT_TRACK_USERS_URL,
            json=payload,
            headers=_headers(),
            timeout=15,
        )
        if resp.status_code >= 400:
            logger.warning("Interakt track user %s: %s", resp.status_code, resp.text[:300])
        else:
            logger.info("Interakt track user ok phone=%s", phone_to_10(phone))
    except Exception:
        logger.exception("Interakt track user failed phone=%s", phone_to_10(phone))


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
