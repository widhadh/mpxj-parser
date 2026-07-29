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


def _safe_str(val):
    if val is None:
        return ''
    return str(val)


def _to_date_str(dt):
    """Convert a Java LocalDateTime / Date to YYYY-MM-DD, or '' if None."""
    if dt is None:
        return ''
    try:
        # LocalDateTime → toLocalDate() → "2003-01-01"
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


def _get_duration_days(task):
    try:
        dur = task.getDuration()
        if dur is not None:
            val = dur.getDuration()
            units = str(dur.getUnits().toString()).upper() if hasattr(dur, 'getUnits') else ''
            if val:
                if 'HOUR' in units:
                    return max(1, round(val / 8))
                if val > 0:
                    return int(val)
    except Exception:
        pass
    # Fallback: compute from start/finish
    try:
        start = task.getStart()
        finish = task.getFinish()
        if start is not None and finish is not None:
            delta = (finish.toLocalDate().toEpochDay() - start.toLocalDate().toEpochDay()) + 1
            return max(1, int(delta))
    except Exception:
        pass
    return 1


def _get_predecessor_info(task):
    """Extract the first predecessor link. Returns (pred_uid, link_type, lag_days)."""
    try:
        preds = task.getPredecessors()
        if not preds:
            return '', 'FS', 0
        for pred in preds:
            pred_task = pred.getPredecessorTask()
            if pred_task is None:
                continue
            pred_uid = str(pred_task.getUniqueID())

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

            return pred_uid, link_type, lag_days
    except Exception:
        pass
    return '', 'FS', 0


def _is_summary_task(task):
    try:
        val = task.getSummary()
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
    try:
        parent = task.getParentTask()
        if parent is not None:
            return str(parent.getUniqueID())
    except Exception:
        pass
    return ''


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
                        'aid': str(t.getUniqueID()),
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

                uid = str(task.getUniqueID())
                start = task.getStart()
                finish = task.getFinish()

                # Skip the project summary task (root level, no dates)
                if start is None and finish is None:
                    continue

                parent_uid = _get_parent_uid(task)
                is_summary = _is_summary_task(task)
                predecessor_id, link_type, lag_days = _get_predecessor_info(task)

                activities.append({
                    'asta_id': uid,
                    'name': str(name),
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
