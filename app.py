"""
MPXJ Microservice — parses native Asta Powerproject (.pp) files using the
open-source MPXJ library and returns structured JSON.

Deploy to Render / any Python host:
  pip install -r requirements.txt
  python app.py

Exposes:
  POST /parse  (multipart/form-data, field: file)  →  JSON { activities, baselines }
"""

import os
import io
import traceback
from flask import Flask, request, jsonify
from mpxj.reader import MPXJReader

app = Flask(__name__)

# ── Link type mapping ─────────────────────────────────────────────────────────
# MPXJ RelationType enum → our canonical FS/SS/FF/SF
LINK_TYPE_MAP = {
    'FINISH_START': 'FS',
    'START_START': 'SS',
    'FINISH_FINISH': 'FF',
    'START_FINISH': 'SF',
}


def _safe_str(val):
    """Safely convert any value to string, returning '' for None."""
    if val is None:
        return ''
    return str(val)


def _to_date_str(dt):
    """Convert a datetime to YYYY-MM-DD string, or '' if None."""
    if dt is None:
        return ''
    try:
        return dt.strftime('%Y-%m-%d')
    except (AttributeError, ValueError):
        return ''


def _get_duration_days(task):
    """Extract duration in days from a task, with fallback to date range."""
    try:
        dur = task.get_duration()
        if dur is not None:
            val = dur.get_duration()
            units = str(dur.get_units()) if hasattr(dur, 'get_units') else ''
            # MPXJ returns duration in hours by default for many formats
            if 'HOUR' in units.upper() and val:
                return max(1, round(val / 8))
            if val and val > 0:
                return val
    except Exception:
        pass
    # Fallback: compute from start/finish
    try:
        start = task.get_start()
        finish = task.get_finish()
        if start and finish:
            delta = (finish - start).days + 1
            return max(1, delta)
    except Exception:
        pass
    return 1


def _get_predecessor_info(task, task_by_uid):
    """Extract the first predecessor link from a task.
    Returns (predecessor_id, link_type, lag_days)."""
    try:
        preds = task.get_predecessors()
        if not preds:
            return '', 'FS', 0
        for pred in preds:
            pred_task = pred.get_predecessor_task()
            if pred_task is None:
                continue
            pred_uid = str(pred_task.get_unique_id())
            # Map link type
            link_type_raw = str(pred.get_type())
            link_type = 'FS'
            for key, val in LINK_TYPE_MAP.items():
                if key in link_type_raw.upper():
                    link_type = val
                    break
            # Extract lag
            lag_days = 0
            try:
                lag = pred.get_lag()
                if lag is not None:
                    lag_val = lag.get_duration()
                    lag_units = str(lag.get_units()) if hasattr(lag, 'get_units') else ''
                    if lag_val:
                        if 'HOUR' in lag_units.upper():
                            lag_days = round(lag_val / 8)
                        else:
                            lag_days = lag_val
            except Exception:
                pass
            return pred_uid, link_type, lag_days
    except Exception:
        pass
    return '', 'FS', 0


def _is_summary_task(task):
    """Determine if a task is a summary task."""
    try:
        val = task.get_summary()
        if val is not None:
            return bool(val)
    except Exception:
        pass
    return False


def _get_outline_level(task):
    """Get the WBS/outline level for a task."""
    try:
        level = task.get_outline_level()
        if level is not None:
            return str(level)
    except Exception:
        pass
    return ''


def _get_parent_uid(task):
    """Get the unique ID of the parent task, if any."""
    try:
        parent = task.get_parent_task()
        if parent is not None:
            return str(parent.get_unique_id())
    except Exception:
        pass
    return ''


def _extract_baselines(project):
    """Extract baseline snapshots from the project."""
    baselines = []
    try:
        bl_list = project.get_baselines()
        if not bl_list:
            return baselines
        for bl in bl_list:
            bl_tasks = []
            try:
                for t in bl.get_tasks():
                    bl_tasks.append({
                        'aid': str(t.get_unique_id()),
                        'sd': _to_date_str(t.get_start()),
                        'ed': _to_date_str(t.get_finish()),
                        'od': _to_date_str(t.get_start()),
                    })
            except Exception:
                pass
            if bl_tasks:
                baselines.append({
                    'name': _safe_str(bl.get_name()) or f'Baseline {len(baselines) + 1}',
                    'date': _to_date_str(bl.get_start()),
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
        project = MPXJReader.read(tmp_path)

        tasks = list(project.get_tasks())
        task_by_uid = {}
        for t in tasks:
            try:
                task_by_uid[str(t.get_unique_id())] = t
            except Exception:
                pass

        activities = []
        for task in tasks:
            try:
                name = task.get_name()
                if not name:
                    continue

                uid = str(task.get_unique_id())
                start = task.get_start()
                finish = task.get_finish()

                # Skip the project summary task (root level, no dates)
                if not start and not finish:
                    continue

                parent_uid = _get_parent_uid(task)
                is_summary = _is_summary_task(task)
                predecessor_id, link_type, lag_days = _get_predecessor_info(task, task_by_uid)

                activities.append({
                    'asta_id': uid,
                    'name': name,
                    'start_date': _to_date_str(start),
                    'end_date': _to_date_str(finish) or _to_date_str(start),
                    'duration_days': _get_duration_days(task),
                    'wbs_level': _get_outline_level(task),
                    'parent_asta_id': parent_uid,
                    'is_summary': is_summary,
                    'predecessor_asta_id': predecessor_id,
                    'link_type': link_type,
                    'lag_days': lag_days,
                })
            except Exception:
                continue

        baselines = _extract_baselines(project)

        # Compute project date range
        all_starts = [a['start_date'] for a in activities if a['start_date']]
        all_ends = [a['end_date'] for a in activities if a['end_date']]
        project_start = min(all_starts) if all_starts else None
        project_end = max(all_ends) if all_ends else None

        return jsonify({
            'activities': activities,
            'project_start': project_start,
            'project_end': project_end,
            'baselines': baselines,
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
