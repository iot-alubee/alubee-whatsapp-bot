"""JMD/MD Online vs Offline — persisted in Firestore ``approver_status``."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

COLLECTION = "approver_status"


def _doc_id(wa_id: str) -> str:
    return (wa_id or "").strip().lower()


def status_ref(db, wa_id: str):
    return db.collection(COLLECTION).document(_doc_id(wa_id))


def get_availability(db, wa_id: str) -> str:
    """``online`` or ``offline``. Missing Firestore doc = **online** (default)."""
    if not wa_id:
        return "online"
    snap = status_ref(db, wa_id).get()
    if not snap.exists:
        return "online"
    raw = (snap.to_dict() or {}).get("availability", "online")
    return "offline" if str(raw).strip().lower() == "offline" else "online"


def ensure_online_default(db, wa_id: str, *, role: str = "") -> None:
    """Create approver_status only when missing — keeps default online without overwriting offline."""
    if not wa_id:
        return
    ref = status_ref(db, wa_id)
    if ref.get().exists:
        return
    set_availability(db, wa_id, "online", role=role)


def is_offline(db, wa_id: str) -> bool:
    return get_availability(db, wa_id) == "offline"


def set_availability(db, wa_id: str, availability: str, *, role: str = "") -> None:
    av = "offline" if str(availability).strip().lower() == "offline" else "online"
    payload: dict = {
        "availability": av,
        "updated_at": datetime.now(timezone.utc),
    }
    if role:
        payload["role"] = role
    status_ref(db, wa_id).set(payload, merge=True)


def approver_role_for_sender(
    sender: str,
    *,
    md: str,
    jmd_i: str,
    jmd_ii: str,
    same_whatsapp: Callable[[str, str], bool],
    test_md: str = "",
) -> str | None:
    """Approver menu role, or None. ``test_md`` gets Online/Offline only — no approval traffic."""
    if md and same_whatsapp(sender, md):
        return "md"
    if jmd_i and same_whatsapp(sender, jmd_i):
        return "jmd_i"
    if jmd_ii and same_whatsapp(sender, jmd_ii):
        return "jmd_ii"
    if test_md and same_whatsapp(sender, test_md):
        return "test_md"
    return None


def is_test_md_sender(
    sender: str, test_md: str, same_whatsapp: Callable[[str, str], bool]
) -> bool:
    return bool(test_md) and same_whatsapp(sender, test_md)


def offline_blocked_message(
    db,
    role: str,
    *,
    md: str,
    jmd_i: str,
    jmd_ii: str,
) -> str | None:
    """If going offline is not allowed, return a polite message; else None."""
    r = (role or "").strip().lower()
    if r == "test_md":
        return None
    if r == "md":
        if (jmd_i and is_offline(db, jmd_i)) or (jmd_ii and is_offline(db, jmd_ii)):
            return "JMD is already offline. Please be Online."
        return None
    if r in ("jmd_i", "jmd_ii"):
        if md and is_offline(db, md):
            return "MD is already offline. Please be Online."
        return None
    return None
