"""Shared bot helpers used by request-type flow modules."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Callable

from firebase_admin import firestore

logger = logging.getLogger(__name__)

_REQUESTS_QUERY_LIMIT = 200
_USER_CACHE_TTL_SEC = 300
_user_cache: dict[str, tuple[float, bool, dict | None]] = {}

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


def query_requests_by_type(firestore_db, req_type: str, *, limit: int | None = None):
    """Recent requests of one type (avoids streaming the whole collection)."""
    cap = limit or _REQUESTS_QUERY_LIMIT
    coll = firestore_db.collection("requests")
    req_type = (req_type or "").strip().upper()
    try:
        q = (
            coll.where("type", "==", req_type)
            .order_by("requested_datetime", direction=firestore.Query.DESCENDING)
            .limit(cap)
        )
        return list(q.stream())
    except Exception as e:
        logger.warning("Firestore requests query failed (%s): %s", req_type, e)
        return list(coll.where("type", "==", req_type).limit(cap).stream())


def query_requests_for_employee(
    firestore_db, req_type: str, employee: str, *, limit: int = 30
):
    """Requests for one employee + type (indexed query, then filtered fallback)."""
    sw = _require("same_whatsapp", same_whatsapp)
    req_type = (req_type or "").strip().upper()
    coll = firestore_db.collection("requests")
    for emp_key in (employee, (employee or "").strip().lower()):
        if not emp_key:
            continue
        try:
            snaps = list(
                coll.where("type", "==", req_type)
                .where("employee", "==", emp_key)
                .limit(limit)
                .stream()
            )
            if snaps:
                return snaps
        except Exception:
            continue
    out = []
    for snap in query_requests_by_type(firestore_db, req_type, limit=limit * 4):
        d = snap.to_dict() or {}
        if sw(d.get("employee"), employee):
            out.append(snap)
        if len(out) >= limit:
            break
    return out


def get_user_record(sender: str) -> tuple[bool, dict | None]:
    """Cached Firestore users/{wa_id} read (one read per sender per 5 min)."""
    _db = _require("db", db)
    key = (sender or "").strip()
    now = time.monotonic()
    cached = _user_cache.get(key)
    if cached and cached[0] > now:
        return cached[1], cached[2]
    snap = _db.collection("users").document(key).get()
    exists = snap.exists
    data = snap.to_dict() if exists else None
    _user_cache[key] = (now + _USER_CACHE_TTL_SEC, exists, data)
    return exists, data


def request_still_pending_approval(d: dict) -> bool:
    """True if approval pipeline is not finished (incl. MD offline bypass)."""
    for key in ("manager_status", "jmd_status", "md_status"):
        if (d.get(key) or "").strip().upper() == "DENIED":
            return False
    if d.get("md_offline_bypass"):
        return False
    md = (d.get("md_status") or "").strip().upper()
    if md in ("APPROVED", "OFFLINE"):
        return False
    jmd = (d.get("jmd_status") or "").strip().upper()
    if jmd in ("PENDING", "AWAITING_MANAGER", "AWAITING_JMD"):
        return True
    if d.get("visitor_dual_jmd"):
        for fld in ("jmd_i_status", "jmd_ii_status"):
            st = (d.get(fld) or "").strip().upper()
            if st in ("PENDING", ""):
                return True
        if md == "PENDING":
            return True
        return False
    return md == "PENDING"


def find_open_request(employee: str, request_type: str) -> dict | None:
    """Open request of given type still in approval pipeline."""
    _db = _require("db", db)
    req_type = (request_type or "").strip().upper()
    for snap in query_requests_for_employee(_db, req_type, employee):
        d = snap.to_dict() or {}
        if request_still_pending_approval(d):
            return d
    return None
