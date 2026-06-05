"""Shared bot helpers used by request-type flow modules."""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]

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


def _ist_now() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def _leave_calendar_months(ref: datetime | None = None) -> tuple[tuple[int, int], tuple[int, int]]:
    """((prev_year, prev_month), (curr_year, curr_month)) in IST."""
    ref = ref or _ist_now()
    y, m = ref.year, ref.month
    if m == 1:
        return (y - 1, 12), (y, m)
    return (y, m - 1), (y, m)


def _parse_ddmmy(text: str) -> date | None:
    raw = (text or "").strip()
    if not raw:
        return None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def expand_leave_date_range(from_s: str, to_s: str) -> list[str]:
    start = _parse_ddmmy(from_s)
    end = _parse_ddmmy(to_s or from_s)
    if not start or not end:
        return []
    if end < start:
        start, end = end, start
    out: list[str] = []
    d = start
    while d <= end:
        out.append(d.strftime("%d-%m-%Y"))
        d += timedelta(days=1)
    return out


def import_leave_doc_id(employee_id: str, year: int, month: int) -> str:
    return f"IMPORT_{(employee_id or '').strip().upper()}_{year}_{month:02d}"


def query_leave_requests_for_employee_id(
    firestore_db,
    employee_id: str,
    *,
    employee_wa: str = "",
    limit: int = 200,
):
    """LEAVE requests for one employee (by employee_id and/or WhatsApp id)."""
    eid = (employee_id or "").strip().upper()
    wa = (employee_wa or "").strip()
    if not eid and not wa:
        return []

    coll = firestore_db.collection("requests")
    found: dict[str, object] = {}

    def _add(snaps) -> None:
        for snap in snaps:
            found[snap.id] = snap

    if eid:
        try:
            _add(
                coll.where("type", "==", "LEAVE")
                .where("employee_id", "==", eid)
                .limit(limit)
                .stream()
            )
        except Exception as e:
            logger.warning("Firestore leave query employee_id=%s: %s", eid, e)

    if wa:
        try:
            _add(
                coll.where("type", "==", "LEAVE")
                .where("employee", "==", wa)
                .limit(limit)
                .stream()
            )
        except Exception as e:
            logger.warning("Firestore leave query employee=%s: %s", wa, e)

    if found:
        return list(found.values())

    out = []
    for snap in query_requests_by_type(firestore_db, "LEAVE", limit=limit * 4):
        d = snap.to_dict() or {}
        match_id = eid and (d.get("employee_id") or "").strip().upper() == eid
        match_wa = wa and (d.get("employee") or "").strip() == wa
        if match_id or match_wa:
            out.append(snap)
        if len(out) >= limit:
            break
    return out


def _count_leave_days_in_month_from_doc(d: dict, year: int, month: int) -> int:
    """Day count from leave_dates / from-to on one request document."""
    month_key = f"{year}-{month:02d}"
    if (d.get("source") or "").strip().lower() == "imported_history":
        hist = (d.get("history_month") or "").strip()
        if hist == month_key:
            return int(d.get("leave_days") or len(d.get("leave_dates") or []) or 0)
    dates = d.get("leave_dates") or []
    if dates:
        n = 0
        for ds in dates:
            parsed = _parse_ddmmy(str(ds))
            if parsed and parsed.year == year and parsed.month == month:
                n += 1
        return n
    from_d = _parse_ddmmy(d.get("leave_from_date"))
    to_d = _parse_ddmmy(d.get("leave_to_date") or d.get("leave_from_date"))
    if not from_d:
        return 0
    if not to_d:
        to_d = from_d
    n = 0
    cur = from_d
    while cur <= to_d:
        if cur.year == year and cur.month == month:
            n += 1
        cur += timedelta(days=1)
    return n


def count_leave_days_in_month_from_dates(
    leave_dates: list[str],
    year: int,
    month: int,
) -> int:
    """Days from a date list (DD-MM-YYYY) falling in the given month."""
    n = 0
    for ds in leave_dates or []:
        parsed = _parse_ddmmy(str(ds))
        if parsed and parsed.year == year and parsed.month == month:
            n += 1
    return n


def _leave_days_in_month(
    d: dict, year: int, month: int, *, include_pending: bool = False
) -> int:
    """Leave day count in month. Last month: approved only. Current: approved + pending."""
    jmd_st = (d.get("jmd_status") or "").strip().upper()
    if jmd_st == "DENIED":
        return 0
    if jmd_st != "APPROVED":
        if not include_pending or jmd_st not in ("PENDING", "AWAITING_MANAGER"):
            return 0
    return _count_leave_days_in_month_from_doc(d, year, month)


def _leave_month_days_count(
    firestore_db,
    employee_id: str,
    year: int,
    month: int,
    *,
    employee_wa: str = "",
    include_pending: bool = False,
    exclude_request_id: str = "",
) -> int:
    eid = (employee_id or "").strip().upper()
    exclude = (exclude_request_id or "").strip()
    seen: set[str] = set()
    total = 0
    for snap in query_leave_requests_for_employee_id(
        firestore_db, eid, employee_wa=employee_wa
    ):
        if exclude and snap.id == exclude:
            continue
        seen.add(snap.id)
        total += _leave_days_in_month(
            snap.to_dict() or {}, year, month, include_pending=include_pending
        )

    if eid:
        imp_id = import_leave_doc_id(eid, year, month)
        if imp_id not in seen and imp_id != exclude:
            imp_snap = firestore_db.collection("requests").document(imp_id).get()
            if imp_snap.exists:
                total += _leave_days_in_month(
                    imp_snap.to_dict() or {}, year, month, include_pending=include_pending
                )
    return total


def get_employee_leave_counts(
    employee_id: str,
    *,
    employee_wa: str = "",
    firestore_db=None,
    reference: datetime | None = None,
    exclude_request_id: str = "",
    extra_current_month_days: int = 0,
) -> tuple[int, int]:
    """
    Leave days in last and current month (IST) from Firestore `requests`.

    Last month: approved only.
    Current month: approved + pending (awaiting approval).
    """
    _db = firestore_db or _require("db", db)
    (prev_y, prev_m), (curr_y, curr_m) = _leave_calendar_months(reference)
    last_month = _leave_month_days_count(
        _db,
        employee_id,
        prev_y,
        prev_m,
        employee_wa=employee_wa,
        include_pending=False,
        exclude_request_id=exclude_request_id,
    )
    current_month = _leave_month_days_count(
        _db,
        employee_id,
        curr_y,
        curr_m,
        employee_wa=employee_wa,
        include_pending=True,
        exclude_request_id=exclude_request_id,
    )
    current_month += max(0, int(extra_current_month_days or 0))
    return last_month, current_month
