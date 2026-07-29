"""
MPXJ Microservice — parses native Asta Powerproject (.pp) files using the
open-source MPXJ Java library via JPype, returns structured JSON.

Deploy to Render / any Python host:
  pip install -r requirements.txt
  gunicorn -w 2 -b 0.0.0.0:5000 --timeout 120 app:app

Exposes:
  POST /parse  (multipart/form-data, field: file)  →  JSON { activities, baselines }
"""

import os
import traceback

import jpype
import mpxj

# Start the JVM once at module import — each gunicorn worker runs this independently
jpype.startJVM()

from org.mpxj.reader import UniversalProjectReader

from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)


# ── Link type mapping ─────────────────────────────────────────────────────────
LINK_TYPE_MAP = {
    'FINISH_START': 'FS',
    'START_START': 'SS',
    'FINISH_FINISH': 'FF',
    'START_FINISH': 'SF',
}

# ── Constraint type mapping ──────────────────────────────────────────────────
def _get_asta_utid(task):
    """Return the Asta Powerproject Unique Task ID (UTID).

    MPXJ exposes this via getActivityID() — which for Powerproject files is
    populated from the programme's "Unique Task ID" attribute (the alphanumeric
    identifier shown in Asta's ID column). getUniqueID() returns MPXJ's own
    internal integer ID, NOT the Asta UTID.

    Falls back to getUniqueID() if the file doesn't populate Activity ID
    (e.g. MS Project / Primavera files).
    """
    try:
        aid = task.getActivityID()
        if aid is not None:
            s = str(aid).strip()
            if s:
                return s
    except Exception:
        pass
    try:
        return str(task.getUniqueID())
    except Exception:
        return ''


def _constraint_type(task):
    try:
        c = task.getConstraint()
        if c is not None:
            return str(c.toString())
    except Exception:
        pass
    return ''


def _safe_str(val):
    if val is None:
        return ''
    return str(val)


def _to_date_str(dt):
    """Convert a Java LocalDateTime / Date to YYYY-MM-DD, or '' if None."""
    if dt is None:
        return ''
    try:
        return str(dt.toLocalDate().toString())
    except Exception:
        pass
    try:
        s = str(dt.toString())
        if len(s) >= 10 and s[4] == '-' and s[7] == '-':
            return s[:10]
    except Exception:
        pass
    return ''


def _date_span_days(task):
    """Return the inclusive calendar-day span from start to finish, or None."""
    try:
        start = task.getStart()
        finish = task.getFinish()
        if start is not None and finish is not None:
            delta = (finish.toLocalDate().toEpochDay() - start.toLocalDate().toEpochDay()) + 1
            if delta > 0:
                return int(delta)
    except Exception:
        pass
    return None


def _mpxj_duration_to_days(dur):
    """Convert an MPXJ Duration object to whole days, handling hours."""
    if dur is None:
        return None
    try:
        val = dur.getDuration()
        if not val or val <= 0:
            return None
        try:
            units = str(dur.getUnits().toString()).upper()
        except Exception:
            units = ''
        if 'HOUR' in units:
            return max(1, round(val / 8))
        if 'MINUTE' in units:
            return max(1, round(val / 60 / 8))
        return max(1, int(val))
    except Exception:
        return None


def _get_duration_days(task):
    """Duration in calendar days, derived from the start→finish date span
    (which matches what the Gantt displays). Falls back to the MPXJ duration
    value (converting hours→days) only when no dates are available."""
    span = _date_span_days(task)
    if span:
        return span
    days = _mpxj_duration_to_days(task.getDuration())
    return days if days else 1


def _get_actual_duration_days(task, duration_days):
    """Actual worked days, derived from percentage complete so it's always
    consistent with duration_days (never in hours)."""
    pct = _get_percentage_complete(task)
    if pct >= 100:
        return duration_days
    if pct > 0:
        return round(duration_days * pct / 100)
    return 0


def _get_remaining_duration_days(duration_days, actual_duration_days):
    return max(0, duration_days - actual_duration_days)


def _get_predecessors(task):
    """Extract ALL predecessor links, not just the first."""
    links = []
    try:
        preds = task.getPredecessors()
        if not preds:
            return links
        for pred in preds:
            pred_task = pred.getPredecessorTask()
            if pred_task is None:
                continue
            # Use the Asta UTID (getActivityID) for linking, with getUniqueID as fallback
            pred_uid = _get_asta_utid(pred_task)
            pred_id = str(pred_task.getID())

            link_type_raw = str(pred.getType())
            link_type = 'FS'
            for key, val in LINK_TYPE_MAP.items():
                if key in link_type_raw.upper():
                    link_type = val
                    break

            lag_days = 0
            try:
                lag = pred.getLag()
                if lag is not None:
                    lag_val = lag.getDuration()
                    lag_units = str(lag.getUnits().toString()).upper() if hasattr(lag, 'getUnits') else ''
                    if lag_val:
                        if 'HOUR' in lag_units:
                            lag_days = round(lag_val / 8)
                        else:
                            lag_days = int(lag_val)
            except Exception:
                pass

            links.append({
                'pred_unique_id': pred_uid,
                'pred_id': pred_id,
                'link_type': link_type,
                'lag_days': lag_days,
            })
    except Exception:
        pass
    return links


def _is_summary_task(task):
    try:
        val = task.getSummary()
        if val is not None:
            return bool(val)
    except Exception:
        pass
    return False


def _is_milestone(task):
    try:
        val = task.getMilestone()
        if val is not None:
            return bool(val)
    except Exception:
        pass
    return False


def _is_critical(task):
    try:
        val = task.getCritical()
        if val is not None:
            return bool(val)
    except Exception:
        pass
    return False


def _get_outline_level(task):
    try:
        level = task.getOutlineLevel()
        if level is not None:
            return str(level)
    except Exception:
        pass
    return ''


def _get_parent_uid(task):
    """Return the parent's Asta UTID so it matches the child's asta_id."""
    try:
        parent = task.getParentTask()
        if parent is not None:
            return _get_asta_utid(parent)
    except Exception:
        pass
    return ''


def _get_percentage_complete(task):
    try:
        pct = task.getPercentageComplete()
        if pct is not None:
            return float(pct)
    except Exception:
        pass
    return 0.0


def _get_priority(task):
    try:
        p = task.getPriority()
        if p is not None:
            return str(p.toString())
    except Exception:
        pass
    return ''


def _get_cost(task):
    try:
        cost = task.getCost()
        if cost is not None:
            return float(cost)
    except Exception:
        pass
    return 0.0


def _get_calendar_name(task):
    try:
        cal = task.getCalendar()
        if cal is not None:
            return str(cal.getName())
    except Exception:
        pass
    return ''


def _extract_working_days(project):
    """Read the working day-of-week pattern (0=Sun..6=Sat) from the
    programme's default calendar. Falls back to Mon–Fri."""
    default = [1, 2, 3, 4, 5]
    try:
        cals = list(project.getCalendars())
    except Exception:
        return default
    if not cals:
        return default

    default_cal = None
    for c in cals:
        try:
            if c.isDefault():
                default_cal = c
                break
        except Exception:
            pass
    if default_cal is None:
        default_cal = cals[0]

    # getWorkingDays() → 7-element array indexed Sunday(0)..Saturday(6),
    # each element one of Day.WORKING / Day.NON_WORKING / Day.PARENT.
    try:
        wd = default_cal.getWorkingDays()
        if wd and len(wd) == 7:
            working = []
            for i, d in enumerate(wd):
                s = str(d.toString()).upper()
                if 'WORKING' in s and 'NON' not in s:
                    working.append(i)
            if working:
                return working
    except Exception:
        pass

    return default


def _get_notes(task):
    try:
        notes = task.getNotes()
        if notes is not None:
            return str(notes)
    except Exception:
        pass
    return ''


def _map_resource_type(res):
    """Map MPXJ ResourceType → 'permanent' or 'temporary'.

    Asta Powerproject distinguishes permanent resources (labour, plant —
    allocated for a duration) from consumable/temporary resources (materials —
    consumed in quantity). MPXJ exposes this via ResourceType:
        LABOR / EQUIPMENT → 'permanent'
        MATERIAL          → 'temporary'  (consumable)
    """
    try:
        rt = res.getType()
        if rt is not None:
            s = str(rt.toString()).upper()
            if 'MATERIAL' in s:
                return 'temporary'
            if 'LABOR' in s or 'LABOUR' in s or 'EQUIPMENT' in s or 'WORK' in s:
                return 'permanent'
            if 'COST' in s:
                return 'temporary'
    except Exception:
        pass
    return 'permanent'


def _get_resource_assignment_units(assign):
    """Best-effort extraction of the quantity allocated to a task.

    Returns a number (units) — the allocation for this assignment.  MPXJ stores
    this as a percentage (e.g. 100 = 1 full resource), so we convert to whole
    units when possible.
    """
    # getUnits() → allocation percentage (100 = 1.0 resource)
    try:
        u = assign.getUnits()
        if u is not None:
            val = float(u)
            if val and val > 0:
                # Convert percentage to whole units (100% = 1, 200% = 2)
                if val >= 100:
                    return round(val / 100, 2)
                return round(val / 100, 2)
    except Exception:
        pass
    # getAmount() → absolute amount for consumable resources
    try:
        amt = assign.getAmount()
        if amt is not None:
            val = float(amt)
            if val and val > 0:
                return round(val, 2)
    except Exception:
        pass
    return 1


def _get_resource_names(task):
    """Extract resource assignment names (backward-compatible)."""
    names = []
    try:
        assignments = task.getResourceAssignments()
        if not assignments:
            return names
        for assign in assignments:
            try:
                res = assign.getResource()
                if res is not None:
                    name = str(res.getName())
                    if name:
                        names.append(name)
            except Exception:
                pass
    except Exception:
        pass
    return names


def _get_resource_assignments(task):
    """Extract full resource assignments with type and allocated quantity.

    Returns a list of dicts:
        { name, resource_type ('permanent'|'temporary'), units }
    """
    assignments_out = []
    try:
        assignments = task.getResourceAssignments()
        if not assignments:
            return assignments_out
        for assign in assignments:
            try:
                res = assign.getResource()
                if res is None:
                    continue
                name = str(res.getName())
                if not name:
                    continue
                assignments_out.append({
                    'name': name,
                    'resource_type': _map_resource_type(res),
                    'units': _get_resource_assignment_units(assign),
                })
            except Exception:
                pass
    except Exception:
        pass
    return assignments_out


def _compute_status(task, pct_complete, actual_start, actual_finish):
    """Derive a status from MPXJ actuals + percentage complete."""
    if pct_complete >= 100:
        return 'completed'
    if actual_start is not None and actual_finish is not None:
        return 'completed'
    if pct_complete > 0 or actual_start is not None:
        return 'in_progress'
    return 'pending'


def _extract_baselines(project):
    baselines = []
    try:
        bl_map = project.getBaselines()
        if not bl_map:
            return baselines
        for entry in bl_map.entrySet():
            bl_project = entry.getValue()
            if bl_project is None:
                continue
            bl_tasks = []
            try:
                for t in bl_project.getTasks():
                    bl_tasks.append({
                        'aid': _get_asta_utid(t),
                        'sd': _to_date_str(t.getStart()),
                        'ed': _to_date_str(t.getFinish()),
                        'od': _to_date_str(t.getStart()),
                    })
            except Exception:
                pass
            if bl_tasks:
                try:
                    name = str(bl_project.getProjectProperties().getName()) or f'Baseline {len(baselines) + 1}'
                except Exception:
                    name = f'Baseline {len(baselines) + 1}'
                baselines.append({
                    'name': name,
                    'date': _to_date_str(bl_project.getProjectProperties().getStartDate()),
                    'tasks': bl_tasks,
                })
    except Exception:
        pass
    return baselines


@app.route('/parse', methods=['POST'])
def parse():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    uploaded_file = request.files['file']
    tmp_path = f'/tmp/{uploaded_file.filename}'

    try:
        uploaded_file.save(tmp_path)
        project = UniversalProjectReader().read(tmp_path)

        tasks = list(project.getTasks())

        activities = []
        for task in tasks:
            try:
                name = task.getName()
                if not name:
                    continue

                # ── IDs ──────────────────────────────────────────────────────
                # getActivityID() = Asta UTID (the "Unique Task ID" shown in
                #   Asta's ID column — alphanumeric, permanent, user-visible)
                # getUniqueID() = MPXJ's internal integer unique ID
                # getID() = sequential display/row ID (renumbered by MPXJ)
                asta_utid = _get_asta_utid(task)
                mpxj_uid = _safe_str(task.getUniqueID())

                # ── Dates ─────────────────────────────────────────────────────
                planned_start = task.getStart()
                planned_finish = task.getFinish()
                actual_start = task.getActualStart()
                actual_finish = task.getActualFinish()
                early_start = task.getEarlyStart()
                early_finish = task.getEarlyFinish()
                late_start = task.getLateStart()
                late_finish = task.getLateFinish()
                baseline_start = task.getBaselineStart()
                baseline_finish = task.getBaselineFinish()

                # Skip the project summary task (root level, no dates)
                if planned_start is None and planned_finish is None and actual_start is None:
                    continue

                # ── Progress ────────────────────────────────────────────────
                pct_complete = _get_percentage_complete(task)

                # ── Hierarchy ───────────────────────────────────────────────
                parent_uid = _get_parent_uid(task)
                is_summary = _is_summary_task(task)

                # ── Dependencies (all links) ────────────────────────────────
                predecessors = _get_predecessors(task)
                # First predecessor for backward compat with schema's single-field
                first_pred = predecessors[0] if predecessors else None

                # ── Flags & attributes ──────────────────────────────────────
                is_milestone = _is_milestone(task)
                is_critical = _is_critical(task)
                outline_level = _get_outline_level(task)
                priority = _get_priority(task)
                cost = _get_cost(task)
                calendar_name = _get_calendar_name(task)
                notes = _get_notes(task)
                constraint = _constraint_type(task)
                constraint_date = _to_date_str(task.getConstraintDate())
                resources = _get_resource_names(task)
                resource_assignments = _get_resource_assignments(task)

                # ── Status from actuals ─────────────────────────────────────
                status = _compute_status(task, pct_complete, actual_start, actual_finish)

                # ── Durations (all derived in calendar days, never hours) ──
                duration_days = _get_duration_days(task)
                actual_duration_days = _get_actual_duration_days(task, duration_days)
                remaining_duration_days = _get_remaining_duration_days(duration_days, actual_duration_days)

                # Use the Asta UTID as the primary asta_id, fallback to UniqueID
                asta_id = asta_utid if asta_utid else mpxj_uid

                activities.append({
                    'asta_id': asta_id,
                    'mpxj_unique_id': mpxj_uid,
                    'name': str(name),
                    'start_date': _to_date_str(planned_start),
                    'end_date': _to_date_str(planned_finish) or _to_date_str(planned_start),
                    'actual_start': _to_date_str(actual_start),
                    'actual_finish': _to_date_str(actual_finish),
                    'early_start': _to_date_str(early_start),
                    'early_finish': _to_date_str(early_finish),
                    'late_start': _to_date_str(late_start),
                    'late_finish': _to_date_str(late_finish),
                    'baseline_start': _to_date_str(baseline_start),
                    'baseline_finish': _to_date_str(baseline_finish),
                    'duration_days': duration_days,
                    'actual_duration_days': actual_duration_days,
                    'remaining_duration_days': remaining_duration_days,
                    'percentage_complete': pct_complete,
                    'status': status,
                    'wbs_level': outline_level,
                    'parent_asta_id': parent_uid,
                    'is_summary': is_summary,
                    'is_milestone': is_milestone,
                    'is_critical': is_critical,
                    'predecessor_asta_id': first_pred['pred_unique_id'] if first_pred else '',
                    'link_type': first_pred['link_type'] if first_pred else 'FS',
                    'lag_days': first_pred['lag_days'] if first_pred else 0,
                    'all_predecessors': predecessors,
                    'priority': priority,
                    'cost': cost,
                    'calendar_name': calendar_name,
                    'notes': notes,
                    'constraint_type': constraint,
                    'constraint_date': constraint_date,
                    'resources': resources,
                    'resource_assignments': resource_assignments,
                })
            except Exception:
                continue

        baselines = _extract_baselines(project)

        all_starts = [a['start_date'] for a in activities if a['start_date']]
        all_ends = [a['end_date'] for a in activities if a['end_date']]
        project_start = min(all_starts) if all_starts else None
        project_end = max(all_ends) if all_ends else None

        working_days = _extract_working_days(project)

        return jsonify({
            'activities': activities,
            'project_start': project_start,
            'project_end': project_end,
            'baselines': baselines,
            'working_days': working_days,
        })

    except Exception as e:
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'mpxj-parser'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
