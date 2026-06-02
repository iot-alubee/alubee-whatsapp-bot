"""
OD (out-duty) request flow — reason, company vehicle, submit; JMD → MD approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]

from interakt_api import send_list_menu, send_reply_buttons, wa_id_to_phone

logger = logging.getLogger(__name__)

OD_ALREADY_PENDING_MSG = "Your OD request is already pending."

OD_SESSION_STATES = frozenset({
    "WAITING_OD_REASON_PICK",
    "WAITING_OD_REASON_TYPING",
    "WAITING_COMPANY_VEHICLE_YESNO",
    "WAITING_VEHICLE_PICK",
    "WAITING_OD_CONFIRM",
})

OD_REASON_CHOICES = frozenset({"UNIT_I", "UNIT_II", "OTHER"})
COMPANY_VEHICLE_CHOICES = frozenset({"YES", "NO"})
CONFIRM_CHOICES = frozenset({"SUBMIT", "CANCEL", "BACK"})


@dataclass
class OdDeps:
    db: object
    send_to: Callable[[str, str], None]
    session_merge: Callable[..., None]
    session_ref: Callable[[str], object]
    utcnow: Callable
    chat_name: Callable[[str], str]
    same_whatsapp: Callable[[str, str], bool]
    build_approval_chain: Callable[[dict], dict | None]
    notify_jmd: Callable[[str, dict, str], bool]
    go_main_menu: Callable[[str], None]
    awaiting_hi_state: str
    already_pending_msg: str = OD_ALREADY_PENDING_MSG


def is_od_state(state: str | None) -> bool:
    return (state or "") in OD_SESSION_STATES


def try_start(sender: str, deps: OdDeps) -> None:
    if _find_open_od_for_employee(deps, sender):
        deps.send_to(sender, deps.already_pending_msg)
        return
    user = deps.db.collection("users").document(sender).get()
    name = "Employee"
    if user.exists:
        name = user.to_dict().get("name") or name
    deps.session_merge(
        sender,
        state="WAITING_OD_REASON_PICK",
        employee_name=name,
        form_type="OD_REQUEST",
    )
    _send_od_reason_buttons(sender, deps)


def handle(sender: str, incoming: str, session: dict, deps: OdDeps) -> None:
    state = session.get("state")
    choice = incoming.strip().upper().replace(" ", "_")
    reason = (session.get("od_reason") or "").strip()

    if choice == "BACK":
        _handle_back(sender, session, deps)
        return

    if choice == "CANCEL":
        if state == "WAITING_OD_CONFIRM":
            _cancel_flow(sender, deps)
        else:
            deps.send_to(
                sender,
                "You can cancel on the final review screen, or reply BACK step by step.",
            )
        return

    if state == "WAITING_OD_CONFIRM":
        if choice == "SUBMIT":
            _submit_from_session(sender, session, deps)
            return
        if choice not in CONFIRM_CHOICES:
            deps.send_to(sender, _locked_step_hint(session))
        return

    if state == "WAITING_OD_REASON_PICK":
        choice = _normalize_od_reason_choice(incoming)
        if choice not in OD_REASON_CHOICES:
            deps.send_to(sender, "Choose Unit I, Unit II, or Other — or send Hi to start over.")
            return
        if choice == "UNIT_I":
            _prompt_company_vehicle(sender, "Unit I", deps)
        elif choice == "UNIT_II":
            _prompt_company_vehicle(sender, "Unit II", deps)
        else:
            _prompt_od_reason_typing(sender, session, deps)

    elif state == "WAITING_OD_REASON_TYPING":
        if choice in OD_REASON_CHOICES or choice in CONFIRM_CHOICES:
            deps.send_to(sender, _locked_step_hint(session))
            return
        reason_text = incoming.strip()
        if reason_text:
            _prompt_company_vehicle(sender, reason_text, deps, session)
        else:
            deps.send_to(sender, "Please write OD reason, or tap Back.")

    elif state == "WAITING_COMPANY_VEHICLE_YESNO":
        if choice in OD_REASON_CHOICES or choice in CONFIRM_CHOICES:
            deps.send_to(sender, _locked_step_hint(session))
            return
        if choice not in COMPANY_VEHICLE_CHOICES:
            deps.send_to(sender, _locked_step_hint(session))
            return
        if choice == "YES":
            vehicles = _fetch_vehicles(deps)
            if not vehicles:
                deps.session_ref(sender).delete()
                deps.send_to(sender, "No company vehicles available. Send Hi to try again.")
            else:
                ids = [v["vehicle_id"] for v in vehicles]
                deps.session_merge(
                    sender,
                    state="WAITING_VEHICLE_PICK",
                    od_reason=reason,
                    vehicle_ids=ids,
                    uses_company_vehicle=True,
                    employee_name=session.get("employee_name"),
                )
                _send_dynamic_vehicle_list(sender, vehicles, deps)
        else:
            _show_confirm(
                sender,
                {
                    **session,
                    "od_reason": reason,
                    "uses_company_vehicle": False,
                    "company_vehicle_id": "",
                    "company_vehicle": "",
                    "company_vehicle_description": "",
                },
                deps,
            )

    elif state == "WAITING_VEHICLE_PICK":
        if choice in OD_REASON_CHOICES or choice in COMPANY_VEHICLE_CHOICES:
            deps.send_to(sender, _locked_step_hint(session))
            return
        ids = session.get("vehicle_ids") or []
        picked = _resolve_vehicle_pick(deps, incoming, ids)
        if picked:
            _show_confirm(
                sender,
                {
                    **session,
                    "od_reason": reason,
                    "uses_company_vehicle": True,
                    "company_vehicle_id": picked["company_vehicle_id"],
                    "company_vehicle": picked["company_vehicle"],
                    "company_vehicle_description": picked["company_vehicle_description"],
                },
                deps,
            )
        else:
            deps.send_to(
                sender,
                "Invalid selection. Pick from the list (number or ID), or reply BACK.",
            )


def _ist_tzinfo():
    if ZoneInfo:
        return ZoneInfo("Asia/Kolkata")
    return timezone(timedelta(hours=5, minutes=30))


def _ist_today():
    return datetime.now(_ist_tzinfo()).date()


def _requested_datetime_ist_date(d: dict):
    """Calendar date in IST for ``requested_datetime``."""
    val = d.get("requested_datetime")
    if val is None:
        return None
    try:
        if hasattr(val, "timestamp") and callable(val.timestamp):
            dtu = datetime.fromtimestamp(val.timestamp(), tz=timezone.utc)
        elif isinstance(val, datetime):
            dtu = (
                val.replace(tzinfo=timezone.utc)
                if val.tzinfo is None
                else val.astimezone(timezone.utc)
            )
        else:
            return None
        return dtu.astimezone(_ist_tzinfo()).date()
    except Exception:
        return None


def _od_request_is_closed(d: dict) -> bool:
    for key in ("manager_status", "jmd_status", "md_status"):
        if (d.get(key) or "").strip().upper() == "DENIED":
            return True
    if d.get("security_in_at"):
        return True
    return False


def _od_approval_still_pending(d: dict) -> bool:
    """JMD/MD approval not finished (excludes denied and fully MD-approved)."""
    if _od_request_is_closed(d):
        return False
    md = (d.get("md_status") or "").strip().upper()
    if md == "APPROVED":
        return False
    jmd = (d.get("jmd_status") or "").strip().upper()
    if jmd in ("PENDING", "AWAITING_MANAGER", "AWAITING_JMD"):
        return True
    return md == "PENDING"


def _find_open_od_for_employee(deps: OdDeps, employee: str) -> dict | None:
    """Block a new OD only when today's request is still awaiting approval."""
    today = _ist_today()
    for snap in deps.db.collection("requests").stream():
        d = snap.to_dict() or {}
        if (d.get("type") or "").strip().upper() != "OD":
            continue
        if not deps.same_whatsapp(d.get("employee"), employee):
            continue
        if _requested_datetime_ist_date(d) != today:
            continue
        if not _od_approval_still_pending(d):
            continue
        return d
    return None


def _vehicles_out_ids(deps: OdDeps):
    out = set()
    for snap in deps.db.collection("requests").stream():
        d = snap.to_dict() or {}
        if (d.get("type") or "").upper() != "OD":
            continue
        vid = (d.get("company_vehicle_id") or "").strip().upper()
        if vid and d.get("security_out_at") and not d.get("security_in_at"):
            out.add(vid)
    return out


def _fetch_vehicles(deps: OdDeps):
    out_ids = _vehicles_out_ids(deps)
    available = []
    for snap in deps.db.collection("vehicles").stream():
        d = snap.to_dict() or {}
        if d.get("active") is False:
            continue
        vid = (d.get("vehicle_id") or snap.id or "").strip().upper()
        if not vid or vid in out_ids:
            continue
        available.append({
            "vehicle_id": vid,
            "vehicle": (d.get("vehicle") or "").strip(),
            "description": (d.get("description") or "").strip(),
        })
    available.sort(key=lambda v: v.get("description") or v.get("vehicle_id") or "")
    return available


def _resolve_vehicle_pick(deps: OdDeps, incoming: str, vehicle_ids: list):
    raw = (incoming or "").strip().upper()
    if not raw or not vehicle_ids:
        return None
    if raw in vehicle_ids:
        idx = vehicle_ids.index(raw)
    elif raw.isdigit():
        n = int(raw)
        if n < 1 or n > len(vehicle_ids):
            return None
        idx = n - 1
    else:
        low = incoming.strip().lower()
        match = [i for i, v in enumerate(vehicle_ids) if v.lower() == low]
        if not match:
            return None
        idx = match[0]
    vid = vehicle_ids[idx]
    snap = deps.db.collection("vehicles").document(vid).get()
    if not snap.exists:
        return None
    d = snap.to_dict() or {}
    return {
        "company_vehicle_id": vid,
        "company_vehicle": (d.get("vehicle") or "").strip(),
        "company_vehicle_description": (d.get("description") or "").strip(),
    }


def _employee_name_for(deps: OdDeps, sender: str, session: dict | None) -> str:
    if session and session.get("employee_name"):
        return deps.chat_name(session["employee_name"])
    user = deps.db.collection("users").document(sender).get()
    if user.exists:
        return deps.chat_name(user.to_dict().get("name"))
    return "Employee"


def _list_rows(*items: tuple[str, str]) -> list[dict[str, str]]:
    return [{"id": row_id, "title": title[:24]} for row_id, title in items]


def _send_od_reason_buttons(wa_id: str, deps: OdDeps) -> None:
    rows = _list_rows(
        ("unit_i", "Unit I"),
        ("unit_ii", "Unit II"),
        ("other", "Other"),
        ("back", "Back"),
    )
    try:
        send_list_menu(
            wa_id_to_phone(wa_id),
            "OD reason:",
            rows,
            button_label="Select reason",
            section_title="Reason",
            callback_data="od-reason",
        )
    except Exception:
        logger.exception("OD reason list failed")
        deps.send_to(
            wa_id,
            "OD reason:\n1. Unit I\n2. Unit II\n3. Other\n\nReply 1, 2, 3, or BACK for menu.",
        )


def _send_company_vehicle_buttons(wa_id: str, reason: str, deps: OdDeps) -> None:
    body = f"{reason}\n\nCompany vehicle?"
    try:
        send_reply_buttons(
            wa_id_to_phone(wa_id),
            body,
            [("YES", "YES"), ("NO", "NO"), ("BACK", "Back")],
            callback_data="company-vehicle",
        )
    except Exception:
        logger.exception("company vehicle buttons failed")
        deps.send_to(wa_id, f"{body}\n\nReply YES, NO, or BACK.")


def _send_dynamic_vehicle_list(wa_id: str, vehicles: list, deps: OdDeps) -> None:
    n = len(vehicles)
    if n == 0:
        return
    lines = [f"Select company vehicle ({n} available; reply with number or ID):\n"]
    for i, v in enumerate(vehicles, start=1):
        label = v.get("description") or v.get("vehicle_id")
        lines.append(f"{i}. {label} ({v['vehicle_id']})")
    lines.append("\nReply BACK to go back.")
    deps.send_to(wa_id, "\n".join(lines))


def _cancel_flow(sender: str, deps: OdDeps) -> None:
    deps.session_merge(sender, state=deps.awaiting_hi_state)
    deps.send_to(sender, "OD cancelled.\nSend Hi when you need the menu.")


def _build_od_summary(session: dict) -> str:
    reason = (session.get("od_reason") or "—").strip()
    uses_cv = session.get("uses_company_vehicle")
    lines = [
        "Please review your OD request:\n",
        "Request type: OD Request",
        f"Reason: {reason}",
    ]
    if uses_cv is True:
        desc = (session.get("company_vehicle_description") or "").strip()
        vid = (session.get("company_vehicle_id") or "").strip()
        vehicle_line = desc or vid or "—"
        lines.append(f"Company vehicle: Yes — {vehicle_line}")
    elif uses_cv is False:
        lines.append("Company vehicle: No")
    else:
        lines.append("Company vehicle: —")
    lines.append("\nSubmit | Cancel | Back")
    return "\n".join(lines)


def _send_od_confirm(sender: str, session: dict, deps: OdDeps) -> None:
    body = _build_od_summary(session)
    deps.session_merge(sender, state="WAITING_OD_CONFIRM")
    try:
        send_reply_buttons(
            wa_id_to_phone(sender),
            body,
            [("SUBMIT", "Submit"), ("CANCEL", "Cancel"), ("BACK", "Back")],
            callback_data="od-confirm",
        )
    except Exception:
        logger.exception("OD confirm buttons failed")
        deps.send_to(sender, f"{body}\n\nReply SUBMIT, CANCEL, or BACK.")


def _show_confirm(sender: str, session: dict, deps: OdDeps) -> None:
    snap = deps.session_ref(sender).get()
    data = {**(snap.to_dict() if snap.exists else {}), **session}
    deps.session_merge(
        sender,
        state="WAITING_OD_CONFIRM",
        od_reason=data.get("od_reason"),
        uses_company_vehicle=data.get("uses_company_vehicle"),
        company_vehicle_id=data.get("company_vehicle_id") or "",
        company_vehicle=data.get("company_vehicle") or "",
        company_vehicle_description=data.get("company_vehicle_description") or "",
        employee_name=data.get("employee_name"),
        vehicle_ids=data.get("vehicle_ids"),
    )
    fresh = deps.session_ref(sender).get()
    _send_od_confirm(sender, fresh.to_dict() if fresh.exists else data, deps)


def _submit_from_session(sender: str, session: dict, deps: OdDeps) -> None:
    reason = (session.get("od_reason") or "").strip()
    if not reason:
        deps.send_to(sender, "Missing OD reason. Send Hi to start again.")
        return
    _submit(
        sender,
        reason,
        deps,
        uses_company_vehicle=bool(session.get("uses_company_vehicle")),
        company_vehicle_id=session.get("company_vehicle_id") or "",
        company_vehicle=session.get("company_vehicle") or "",
        company_vehicle_description=session.get("company_vehicle_description") or "",
    )


def _submit(
    sender: str,
    reason: str,
    deps: OdDeps,
    *,
    uses_company_vehicle: bool = False,
    company_vehicle_id: str = "",
    company_vehicle: str = "",
    company_vehicle_description: str = "",
) -> None:
    if _find_open_od_for_employee(deps, sender):
        deps.session_ref(sender).delete()
        deps.send_to(sender, deps.already_pending_msg)
        return

    user_doc = deps.db.collection("users").document(sender).get()
    if not user_doc.exists:
        deps.session_ref(sender).delete()
        deps.send_to(sender, "User not registered.\nPlease contact admin.")
        return

    ud = user_doc.to_dict()
    chain = deps.build_approval_chain(ud)
    if not chain:
        deps.session_ref(sender).delete()
        deps.send_to(sender, "Approval chain not configured.\nPlease contact admin.")
        return

    ref = deps.db.collection("requests").document()
    request_id = ref.id
    ref.set({
        "request_id": request_id,
        "requested_datetime": deps.utcnow(),
        "employee": sender,
        "employee_id": ud.get("employee_id") or "",
        "employee_name": ud.get("name") or "Employee",
        "department": ud.get("department") or "",
        "type": "OD",
        "reason": reason,
        "uses_company_vehicle": uses_company_vehicle,
        "company_vehicle_id": company_vehicle_id or "",
        "company_vehicle": company_vehicle or "",
        "company_vehicle_description": company_vehicle_description or "",
        "jmd": chain["jmd"],
        "jmd_route": chain["jmd_route"],
        "md": chain["md"],
        "manager_status": "N/A",
        "jmd_status": "PENDING",
        "md_status": "AWAITING_JMD",
    })
    logger.info("OD created %s jmd_route=%s", request_id, chain["jmd_route"])

    rd = ref.get().to_dict()
    jmd_ok = deps.notify_jmd(chain["jmd"], rd, request_id)

    deps.session_ref(sender).delete()
    msg = "OD is Submitted."
    if uses_company_vehicle and company_vehicle_description:
        msg += f"\nVehicle: {company_vehicle_description}."
    if not jmd_ok:
        route = chain["jmd_route"]
        msg += (
            f"\n\nJMD ({route}) could not be notified on WhatsApp. "
            "Ask them to send Hi to this Alubee number once, then try again or contact admin."
        )
    deps.send_to(sender, msg)


def _normalize_od_reason_choice(incoming: str) -> str:
    choice = incoming.strip().upper().replace(" ", "_")
    if choice == "1":
        return "UNIT_I"
    if choice == "2":
        return "UNIT_II"
    if choice == "3":
        return "OTHER"
    if choice == "UNITII":
        return "UNIT_II"
    return choice


def _go_back_to_od_reason_pick(sender: str, session: dict | None, deps: OdDeps) -> None:
    name = _employee_name_for(deps, sender, session)
    deps.session_merge(sender, state="WAITING_OD_REASON_PICK", employee_name=name)
    _send_od_reason_buttons(sender, deps)


def _go_back_to_company_vehicle(
    sender: str, reason: str, session: dict | None, deps: OdDeps
) -> None:
    name = _employee_name_for(deps, sender, session)
    deps.session_merge(
        sender,
        state="WAITING_COMPANY_VEHICLE_YESNO",
        od_reason=reason,
        employee_name=name,
        uses_company_vehicle=None,
        company_vehicle_id="",
        company_vehicle="",
        company_vehicle_description="",
    )
    _send_company_vehicle_buttons(sender, reason, deps)


def _go_back_from_confirm(sender: str, session: dict, deps: OdDeps) -> None:
    reason = (session.get("od_reason") or "").strip()
    if session.get("uses_company_vehicle") and (
        session.get("company_vehicle_id") or session.get("vehicle_ids")
    ):
        ids = session.get("vehicle_ids") or []
        if session.get("company_vehicle_id") and session["company_vehicle_id"] not in ids:
            ids = list(ids) + [session["company_vehicle_id"]]
        vehicles = _fetch_vehicles(deps)
        if not vehicles:
            _go_back_to_company_vehicle(sender, reason, session, deps)
            return
        deps.session_merge(
            sender,
            state="WAITING_VEHICLE_PICK",
            od_reason=reason,
            vehicle_ids=[v["vehicle_id"] for v in vehicles],
            employee_name=session.get("employee_name"),
            uses_company_vehicle=True,
        )
        _send_dynamic_vehicle_list(sender, vehicles, deps)
        return
    _go_back_to_company_vehicle(sender, reason, session, deps)


def _prompt_od_reason_typing(sender: str, session: dict | None, deps: OdDeps) -> None:
    name = _employee_name_for(deps, sender, session)
    deps.session_merge(sender, state="WAITING_OD_REASON_TYPING", employee_name=name)
    try:
        send_reply_buttons(
            wa_id_to_phone(sender),
            "Please write OD reason:",
            [("BACK", "Back")],
            callback_data="od-reason-type",
        )
    except Exception:
        logger.exception("OD reason typing prompt failed")
        deps.send_to(sender, "Please write OD reason:")


def _prompt_company_vehicle(
    sender: str, reason: str, deps: OdDeps, session: dict | None = None
) -> None:
    name = _employee_name_for(deps, sender, session)
    deps.session_merge(
        sender,
        state="WAITING_COMPANY_VEHICLE_YESNO",
        od_reason=reason,
        employee_name=name,
    )
    _send_company_vehicle_buttons(sender, reason, deps)


def _locked_step_hint(session: dict) -> str:
    state = session.get("state")
    reason = (session.get("od_reason") or "").strip()
    if state == "WAITING_OD_REASON_PICK":
        return "Choose Unit I, Unit II, Other, or Back."
    if state == "WAITING_COMPANY_VEHICLE_YESNO":
        return f"Reason: {reason}. Tap YES, NO, or Back."
    if state == "WAITING_VEHICLE_PICK":
        return "Pick a vehicle from the list, or reply BACK."
    if state == "WAITING_OD_REASON_TYPING":
        return "Write your OD reason, or tap Back."
    if state == "WAITING_OD_CONFIRM":
        return "Tap Submit, Cancel, or Back to review or change your choices."
    return "Use the options for this step, or send Hi to start over."


def _handle_back(sender: str, session: dict, deps: OdDeps) -> None:
    state = session.get("state")
    reason = (session.get("od_reason") or "").strip()

    if state == "WAITING_OD_REASON_PICK":
        deps.go_main_menu(sender)
        return
    if state == "WAITING_OD_REASON_TYPING":
        _go_back_to_od_reason_pick(sender, session, deps)
        return
    if state == "WAITING_COMPANY_VEHICLE_YESNO":
        _go_back_to_od_reason_pick(sender, session, deps)
        return
    if state == "WAITING_VEHICLE_PICK":
        _go_back_to_company_vehicle(sender, reason, session, deps)
        return
    if state == "WAITING_OD_CONFIRM":
        _go_back_from_confirm(sender, session, deps)
        return
    deps.go_main_menu(sender)
