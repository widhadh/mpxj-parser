"""Asta Powerproject calendar exception extraction.

MPXJ's Asta reader does not expose calendar exceptions (bank holidays /
shutdown periods), so schedules parsed from .pp files previously reported no
non-working exception dates — which made every working-day calculation in the
app (S-curve variance, planned work-days, day view) treat bank holidays and
the Christmas shutdown as working days.

Asta stores each calendar's exceptions as a serialised NTEXT blob on its
CALENDAR table row:

    "count: <jdnStart> 0,<jdnEnd> 0,<exceptionTypeId>," ...

one repeating triple per exception range, dates as Julian Day Numbers. The
type ids resolve against the EXCEPTIONN table, whose EXCEPTION_TYPE column
marks 1 = non-working ('Non Working', 'Holiday', 'Weekend', 'School Term -
non-working', ...) and 0 = working ('Working', 'Overtime', 'Weekend
Working', 'School Hols - working', ...). Only non-working ranges produce
exception dates.

This module reads the .pp SQLite database directly (same approach as
asta_links.py), decodes the chosen calendar's exception ranges and returns
the non-working dates as sorted ISO strings. The chosen calendar is the one
the schedule's tasks reference most by name — in practice the project's
default working calendar, which tasks without a calendar of their own
inherit.
"""

import sqlite3
from datetime import date, timedelta

# Julian Day Number 2451545 = 2000-01-01
_JDN_EPOCH = date(2000, 1, 1)
# An exception range longer than this is not a holiday/shutdown — ignore it
_MAX_RANGE_DAYS = 1000


def _jdn_to_date(jdn):
    try:
        d = _JDN_EPOCH + timedelta(days=int(jdn) - 2451545)
    except Exception:
        return None
    if d.year < 1900 or d.year > 2100:
        return None
    return d


def _parse_exception_blob(blob):
    """'148:2457378 0,2457383 0,6,...' -> [(start_jdn, end_jdn, type_id), ...]"""
    if not blob:
        return []
    body = str(blob)
    if ':' in body:
        body = body.split(':', 1)[1]
    parts = [p for p in body.split(',') if p != '']
    ranges = []
    for i in range(0, len(parts) - 2, 3):
        try:
            s = int(parts[i].split(' ')[0])
            e = int(parts[i + 1].split(' ')[0])
            t = int(parts[i + 2])
        except Exception:
            continue
        ranges.append((s, e, t))
    return ranges


def extract_calendar_exceptions(tmp_path, activities):
    """Non-working exception dates (sorted ISO YYYY-MM-DD) of the calendar the
    schedule's tasks use most, read straight from the .pp SQLite database.

    Returns None for non-Asta files (SQLite/Asta tables absent), when the
    exception types cannot be classified, or when no task names a calendar
    the database defines — the caller then keeps its MPXJ-based extraction.
    """
    try:
        conn = sqlite3.connect(tmp_path)
    except Exception:
        return None
    try:
        # Non-working exception type ids (EXCEPTIONN.EXCEPTION_TYPE = 1)
        nonworking_types = set()
        for eid, etype in conn.execute('SELECT ID, EXCEPTION_TYPE FROM EXCEPTIONN'):
            try:
                if int(etype) == 1:
                    nonworking_types.add(int(eid))
            except Exception:
                continue
        if not nonworking_types:
            return None

        # calendar name (lower-cased) -> set of non-working exception dates
        by_name = {}
        for projid, cid, name, blob in conn.execute(
                'SELECT PROJID, ID, NAME, EXCEPTIONS FROM CALENDAR'):
            if not name:
                continue
            key = str(name).strip().lower()
            if key not in by_name:
                by_name[key] = set()
            for s, e, t in _parse_exception_blob(blob):
                if t not in nonworking_types:
                    continue
                start = _jdn_to_date(s)
                end = _jdn_to_date(e)
                if not start or not end or end < start:
                    continue
                if (end - start).days > _MAX_RANGE_DAYS:
                    continue
                d = start
                while d <= end:
                    by_name[key].add(d.isoformat())
                    d += timedelta(days=1)

        if not by_name:
            return None

        # The calendar the tasks reference most — the project's de-facto
        # default working calendar (tasks with no calendar of their own
        # inherit it).
        usage = {}
        for a in activities or []:
            name = (a.get('calendar_name') or '').strip().lower()
            if name:
                usage[name] = usage.get(name, 0) + 1

        best_key = None
        best_count = 0
        for key, count in usage.items():
            if key in by_name and count > best_count:
                best_key = key
                best_count = count
        if not best_key:
            return None
        return sorted(by_name[best_key])
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass

