"""
In-memory sessions: fine for single-instance / local dev.
For Cloud Run with multiple replicas, replace with Redis / Firestore / SQL.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock


@dataclass
class Session:
    mobile_key: str
    state: str = "idle"
    employee_name: str | None = None
    department: str | None = None
    od_reason_code: str | None = None  # U1 | U2 | OTHER
    od_reason_text: str | None = None
    updated_at: datetime = field(default_factory=datetime.utcnow)


_LOCK = Lock()
_STORE: dict[str, Session] = {}
_TTL = timedelta(hours=6)


def _key(from_addr: str) -> str:
    return from_addr.strip().lower()


def get_session(from_addr: str) -> Session | None:
    with _LOCK:
        s = _STORE.get(_key(from_addr))
        if not s:
            return None
        if datetime.utcnow() - s.updated_at > _TTL:
            del _STORE[_key(from_addr)]
            return None
        return s


def upsert_session(from_addr: str, **kwargs) -> Session:
    with _LOCK:
        k = _key(from_addr)
        s = _STORE.get(k)
        if not s:
            s = Session(mobile_key=k)
            _STORE[k] = s
        for name, val in kwargs.items():
            if hasattr(s, name):
                setattr(s, name, val)
        s.updated_at = datetime.utcnow()
        return s


def clear_session(from_addr: str) -> None:
    with _LOCK:
        _STORE.pop(_key(from_addr), None)


# Pending OD approvals: token -> {employee_from, employee_name, department, reason, created_at}
_PENDING_LOCK = Lock()
_PENDING: dict[str, dict] = {}


def pending_put(token: str, payload: dict) -> None:
    with _PENDING_LOCK:
        _PENDING[token] = payload


def pending_pop(token: str) -> dict | None:
    with _PENDING_LOCK:
        return _PENDING.pop(token, None)


def pending_get(token: str) -> dict | None:
    with _PENDING_LOCK:
        return _PENDING.get(token)
