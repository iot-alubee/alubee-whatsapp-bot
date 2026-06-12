"""Expected permission times and hours (management only — not security gate)."""

from __future__ import annotations

import re

_TYPE_LATE_IN = "PERMISSION_LATE_IN"
_TYPE_EARLY_OUT = "PERMISSION_EARLY_OUT"
_TYPE_OTHER = "PERMISSION_OTHER"

MAX_PERMISSION_MINUTES = 4 * 60
SLOT_STEP = 30


def _format_12h(hour24: int, minute: int) -> str:
    period = "AM" if hour24 < 12 else "PM"
    hour12 = hour24 % 12
    if hour12 == 0:
        hour12 = 12
    return f"{hour12}:{minute:02d} {period}"


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


def permission_type_allowed(
    ud: dict | None, permission_shift: str, permission_type_code: str
) -> bool:
    """RS Shift II: Late IN only."""
    if not ud:
        return True
    st = (ud.get("shift_type") or "GS").strip().upper()
    shift = (permission_shift or "I").strip().upper()
    code = (permission_type_code or "").strip().upper()
    if st == "RS" and shift in ("II", "2"):
        return code == _TYPE_LATE_IN
    return True


def _slot_rows(minutes_start: int, minutes_end: int) -> list[str]:
    if minutes_end < minutes_start:
        return []
    labels: list[str] = []
    m = minutes_start
    while m <= minutes_end:
        hour24, minute = divmod(m, 60)
        if hour24 > 23:
            break
        labels.append(_format_12h(hour24, minute))
        m += SLOT_STEP
    return labels


def _late_in_labels(login_hhmm: str) -> list[str]:
    login = _parse_hhmm(login_hhmm)
    if not login:
        return []
    login_m = _minutes_of_day(login[0], login[1])
    return _slot_rows(login_m + SLOT_STEP, login_m + MAX_PERMISSION_MINUTES)


def _early_out_labels(logout_hhmm: str) -> list[str]:
    logout = _parse_hhmm(logout_hhmm)
    if not logout:
        return []
    logout_m = _minutes_of_day(logout[0], logout[1])
    return _slot_rows(
        logout_m - MAX_PERMISSION_MINUTES + SLOT_STEP, logout_m - SLOT_STEP
    )


def _other_out_labels(login_hhmm: str, logout_hhmm: str) -> list[str]:
    login = _parse_hhmm(login_hhmm)
    logout = _parse_hhmm(logout_hhmm)
    if not login or not logout:
        return []
    login_m = _minutes_of_day(login[0], login[1])
    logout_m = _minutes_of_day(logout[0], logout[1])
    start_m = (
        login_m
        if login_m % SLOT_STEP == 0
        else ((login_m // SLOT_STEP) + 1) * SLOT_STEP
    )
    return _slot_rows(start_m, logout_m - SLOT_STEP)


def _other_in_labels(expected_out: str, logout_hhmm: str) -> list[str]:
    out_t = _parse_12h(normalize_permission_time_label(expected_out))
    logout = _parse_hhmm(logout_hhmm)
    if not out_t or not logout:
        return []
    out_m = _minutes_of_day(out_t[0], out_t[1])
    logout_m = _minutes_of_day(logout[0], logout[1])
    return _slot_rows(out_m + SLOT_STEP, min(out_m + MAX_PERMISSION_MINUTES, logout_m))


def _flow_type_key(permission_type_code: str) -> str:
    code = (permission_type_code or "").strip().upper()
    if code == _TYPE_LATE_IN:
        return "late_in"
    if code == _TYPE_EARLY_OUT:
        return "early_out"
    if code == _TYPE_OTHER:
        return "other"
    return ""


def validate_expected_permission_times(
    ud: dict | None,
    *,
    permission_shift: str,
    permission_type_code: str,
    expected_in: str = "",
    expected_out: str = "",
) -> bool:
    if not permission_type_allowed(ud, permission_shift, permission_type_code):
        return False
    bounds = resolve_shift_login_logout(ud, permission_shift)
    if not bounds:
        return False
    login_hhmm, logout_hhmm = bounds
    pt = _flow_type_key(permission_type_code)
    exp_in = normalize_permission_time_label(expected_in)
    exp_out = normalize_permission_time_label(expected_out)
    if pt == "late_in":
        return bool(exp_in) and exp_in in set(_late_in_labels(login_hhmm))
    if pt == "early_out":
        return bool(exp_out) and exp_out in set(_early_out_labels(logout_hhmm))
    if pt == "other":
        if not exp_out or exp_out not in set(_other_out_labels(login_hhmm, logout_hhmm)):
            return False
        return bool(exp_in) and exp_in in set(_other_in_labels(exp_out, logout_hhmm))
    return False


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
