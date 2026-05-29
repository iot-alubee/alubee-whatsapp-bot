"""Shared bot helpers used by request-type flow modules."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable

# Set from main.py after Firestore init.
db = None
send_to: Callable[[str, str], None] | None = None
session_ref: Callable[[str], object] | None = None
session_merge: Callable[..., None] | None = None
utcnow: Callable[[], datetime] | None = None
has_active_whatsapp_session: Callable[[str], bool] | None = None
chat_name: Callable[[str], str] | None = None
same_whatsapp: Callable[[str, str], bool] | None = None


def configure(**kwargs) -> None:
    global db, send_to, session_ref, session_merge, utcnow
    global has_active_whatsapp_session, chat_name, same_whatsapp
    for key, val in kwargs.items():
        if key in globals():
            globals()[key] = val


def _require(name: str, val):
    if val is None:
        raise RuntimeError(f"bot_shared.{name} not configured")
    return val


def digits(value: str) -> str:
    return "".join(c for c in str(value or "").strip() if c.isdigit())


def wa_from_10(mobile: str) -> str:
    d = digits(mobile)
    if len(d) == 10:
        return f"whatsapp:+91{d}"
    if len(d) == 12 and d.startswith("91"):
        return f"whatsapp:+{d}"
    return ""


def wa_from_env(*env_keys: str) -> str:
    """Read Cloud Run / .env approver ids at request time (whatsapp:+91… or digits)."""
    for key in env_keys:
        raw = (os.getenv(key) or "").strip().strip('"').strip("'")
        if not raw:
            continue
        d = digits(raw)
        if len(d) == 10:
            return f"whatsapp:+91{d}"
        if len(d) >= 12 and d.startswith("91"):
            return f"whatsapp:+{d[-12:]}" if len(d) > 12 else f"whatsapp:+{d}"
        if len(d) > 10:
            return f"whatsapp:+91{d[-10:]}"
    return ""


def find_open_request(employee: str, request_type: str) -> dict | None:
    """Open request of given type still in approval pipeline."""
    _db = _require("db", db)
    req_type = (request_type or "").strip().upper()
    for snap in _db.collection("requests").stream():
        d = snap.to_dict() or {}
        if (d.get("type") or "").strip().upper() != req_type:
            continue
        if not _require("same_whatsapp", same_whatsapp)(d.get("employee"), employee):
            continue
        for key in ("manager_status", "jmd_status", "md_status"):
            if (d.get(key) or "").strip().upper() == "DENIED":
                break
        else:
            md = (d.get("md_status") or "").strip().upper()
            if md == "APPROVED":
                continue
            jmd = (d.get("jmd_status") or "").strip().upper()
            if jmd in ("PENDING", "AWAITING_MANAGER", "AWAITING_JMD"):
                return d
            if md == "PENDING":
                return d
    return None
