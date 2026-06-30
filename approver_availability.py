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


def _any_jmd_offline(db, *, jmd_i: str, jmd_ii: str) -> bool:
    seen: set[str] = set()
    for wa in (jmd_i, jmd_ii):
        key = (wa or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if is_offline(db, wa):
            return True
    return False


def set_availability(
    db,
    wa_id: str,
    availability: str,
    *,
    role: str = "",
    enforce_pair_rule: bool = False,
    md: str = "",
    jmd_i: str = "",
    jmd_ii: str = "",
) -> str | None:
    """Persist availability. Returns error message when offline is blocked, else None."""
    av = "offline" if str(availability).strip().lower() == "offline" else "online"
    if av == "offline" and enforce_pair_rule:
        blocked = offline_blocked_message(
            db,
            role,
            md=md,
            jmd_i=jmd_i,
            jmd_ii=jmd_ii,
        )
        if blocked:
            return blocked
    payload: dict = {
        "availability": av,
        "updated_at": datetime.now(timezone.utc),
    }
    if role:
        payload["role"] = role
    status_ref(db, wa_id).set(payload, merge=True)
    return None


def approver_role_for_sender(
    sender: str,
    *,
    md: str,
    jmd_i: str,
    jmd_ii: str,
    same_whatsapp: Callable[[str, str], bool],
) -> str | None:
    """Online/Offline menu role — JMD I (JMD1), JMD II (JMD2), and MD only."""
    if md and same_whatsapp(sender, md):
        return "md"
    if jmd_i and same_whatsapp(sender, jmd_i):
        return "jmd_i"
    if jmd_ii and same_whatsapp(sender, jmd_ii):
        return "jmd_ii"
    return None


def is_availability_approver_wa(
    wa_id: str,
    *,
    md: str,
    jmd_i: str,
    jmd_ii: str,
    same_whatsapp: Callable[[str, str], bool],
) -> bool:
    """True only for configured JMD1 / JMD2 / MD WhatsApp numbers."""
    for configured in (md, jmd_i, jmd_ii):
        if configured and same_whatsapp(wa_id, configured):
            return True
    return False


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
    if r == "md":
        if _any_jmd_offline(db, jmd_i=jmd_i, jmd_ii=jmd_ii):
            return "JMD is already offline, so please be online."
        return None
    if r in ("jmd", "jmd_i", "jmd_ii"):
        if md and is_offline(db, md):
            return "MD is already offline, so please be online."
        return None
    return None
