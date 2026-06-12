"""Expected permission times and hours (management only — not security gate)."""

from __future__ import annotations

import re

_TYPE_LATE_IN = "PERMISSION_LATE_IN"
_TYPE_EARLY_OUT = "PERMISSION_EARLY_OUT"
_TYPE_OTHER = "PERMISSION_OTHER"


def _format_12h(hour24: int, minute: int) -> str:
    period = "AM" if hour24 < 12 else "PM"
    hour12 = hour24 % 12
    if hour12 == 0:
        hour12 = 12
    return f"{hour12}:{minute:02d} {period}"


def permission_time_slot_options() -> list[dict[str, str]]:
    """WhatsApp Flow dropdown: 12:00 AM – 11:30 PM every 30 minutes."""
    rows: list[dict[str, str]] = []
    for hour24 in range(24):
        for minute in (0, 30):
            label = _format_12h(hour24, minute)
            rows.append({"id": label, "title": label})
    return rows


def normalize_permission_time_label(raw: str) -> str:
    """Accept Flow id (t_1000) or '10:00 AM' → canonical '10:00 AM'."""
    s = (raw or "").strip()
    if not s:
        return ""
    parsed = _parse_12h(s)
    if parsed is None:
        return ""
    return _format_12h(parsed[0], parsed[1])


def _parse_12h(text: str) -> tuple[int, int] | None:
    s = (text or "").strip().upper()
    m = re.match(r"^(\d{1,2})\s*:\s*(\d{2})\s*(AM|PM)$", s)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    period = m.group(3)
    if hour < 1 or hour > 12 or minute < 0 or minute > 59:
        return None
    if period == "AM":
        hour24 = 0 if hour == 12 else hour
    else:
        hour24 = 12 if hour == 12 else hour + 12
    return hour24, minute


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    s = (text or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour, minute


def _minutes_of_day(hour24: int, minute: int) -> int:
    return hour24 * 60 + minute


def format_duration_minutes(minutes: int) -> str:
    if minutes <= 0:
        return "—"
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}H {mins}M"
    if hours:
        return f"{hours}H"
    return f"{mins}M"


def resolve_shift_login_logout(
    ud: dict | None, permission_shift: str
) -> tuple[str, str] | None:
    """Regular shift login/logout as HH:MM (24h) from user profile."""
    if not ud:
        return None
    st = (ud.get("shift_type") or "GS").strip().upper()
    shift = (permission_shift or "I").strip().upper()
    if st == "GS":
        login = ud.get("shift_login")
        logout = ud.get("shift_logout")
    elif shift in ("II", "2"):
        login = ud.get("shift2_login")
        logout = ud.get("shift2_logout")
    else:
        login = ud.get("shift1_login")
        logout = ud.get("shift1_logout")
    if not login or not logout:
        return None
    return str(login).strip(), str(logout).strip()


def compute_expected_permission_hours(
    ud: dict | None,
    *,
    permission_shift: str,
    permission_type_code: str,
    expected_in: str = "",
    expected_out: str = "",
) -> tuple[int, str]:
    """
    Hours required from expected times vs regular shift (or out→in for Other).
    Returns (minutes, display e.g. '2H 30M').
    """
    code = (permission_type_code or "").strip().upper()
    bounds = resolve_shift_login_logout(ud, permission_shift)
    if code == _TYPE_LATE_IN:
        exp = _parse_12h(normalize_permission_time_label(expected_in))
        if not exp or not bounds:
            return 0, "—"
        reg = _parse_hhmm(bounds[0])
        if not reg:
            return 0, "—"
        mins = _minutes_of_day(exp[0], exp[1]) - _minutes_of_day(reg[0], reg[1])
        mins = max(0, mins)
        return mins, format_duration_minutes(mins)

    if code == _TYPE_EARLY_OUT:
        exp = _parse_12h(normalize_permission_time_label(expected_out))
        if not exp or not bounds:
            return 0, "—"
        reg = _parse_hhmm(bounds[1])
        if not reg:
            return 0, "—"
        mins = _minutes_of_day(reg[0], reg[1]) - _minutes_of_day(exp[0], exp[1])
        mins = max(0, mins)
        return mins, format_duration_minutes(mins)

    if code == _TYPE_OTHER:
        out_t = _parse_12h(normalize_permission_time_label(expected_out))
        in_t = _parse_12h(normalize_permission_time_label(expected_in))
        if not out_t or not in_t:
            return 0, "—"
        mins = _minutes_of_day(in_t[0], in_t[1]) - _minutes_of_day(
            out_t[0], out_t[1]
        )
        mins = max(0, mins)
        return mins, format_duration_minutes(mins)

    return 0, "—"


def permission_time_lines_for_approval(rd: dict) -> str:
    """Extra lines for JMD/MD approval message."""
    code = (rd.get("permission_type_code") or "").strip().upper()
    exp_in = (rd.get("permission_expected_in") or "").strip()
    exp_out = (rd.get("permission_expected_out") or "").strip()
    hours = (rd.get("permission_hours_required") or "").strip()
    lines: list[str] = []
    if code == _TYPE_LATE_IN and exp_in:
        lines.append(f"Expected IN time: {exp_in}")
    elif code == _TYPE_EARLY_OUT and exp_out:
        lines.append(f"Expected OUT time: {exp_out}")
    elif code == _TYPE_OTHER:
        if exp_out:
            lines.append(f"Expected OUT time: {exp_out}")
        if exp_in:
            lines.append(f"Expected IN time: {exp_in}")
    if hours and hours != "—":
        lines.append(f"Hours Required: {hours}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"
