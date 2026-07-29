import os
import tempfile
import urllib.request
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS

import jpype
import mpxj

jpype.startJVM()
from org.mpxj.reader import UniversalProjectReader

app = Flask(__name__)
CORS(app)


def format_date(java_date):
    if java_date is None:
        return None
    try:
        millis = java_date.getTime()
        return datetime.fromtimestamp(millis / 1000).strftime('%Y-%m-%d')
    except Exception:
        s = str(java_date)
        return s[:10] if len(s) >= 10 else None


def get_duration_days(duration):
    if duration is None:
        return 1
    try:
        val = float(duration.getDuration())
        units = str(duration.getUnits())
        if 'HOUR' in units:
            return max(1, round(val / 8))
        if 'DAY' in units:
            return max(1, round(val))
        if 'WEEK' in units:
            return max(1, round(val * 5))
        return max(1, round(val))
    except Exception:
        return 1


def map_relation_type(rel_type_str):
    s = str(rel_type_str)
    if 'START_TO_START' in s:
        return 'SS'
    if 'FINISH_TO_FINISH' in s:
        return 'FF'
    if 'START_TO_FINISH' in s:
        return 'SF'
    return 'FS'


def parse_pp_file(file_path):
    project = UniversalProjectReader().read(file_path)

    # Build predecessor map from relations
    pred_map = {}
    try:
        relations = project.getRelations()
        for i in range(relations.size()):
            rel = relations.get(i)
            succ = rel.getTargetTask()
            pred = rel.getSourceTask()
            if succ is None or pred is None:
                continue
            succ_id = str(succ.getID())
            if succ_id in pred_map:
                continue
            link_type = map_relation_type(rel.getType())
            lag = rel.getLag()
            lag_days = 0
            if lag is not None:
                try:
                    lag_val = float(lag.getDuration())
                    lag_units = str(lag.getUnits())
                    if 'HOUR' in lag_units:
                        lag_days = round(lag_val / 8)
                    else:
                        lag_days = round(lag_val)
                except Exception:
                    pass
            pred_map[succ_id] = {
                'pred_id': str(pred.getID()),
                'link_type': link_type,
                'lag_days': lag_days,
            }
    except Exception as e:
        print(f'[MPXJ] Warning reading relations: {e}')

    activities = []
    all_tasks = project.getTasks()

    for i in range(all_tasks.size()):
        task = all_tasks.get(i)
        name = str(task.getName()) if task.getName() else ''
        if not name:
            continue

        task_id = str(task.getID()) if task.getID() else str(task.getUniqueID())
        start = format_date(task.getStart())
        if not start:
            continue
        end = format_date(task.getFinish()) or start

        is_summary = False
        try:
            is_summary = bool(task.getSummary())
        except Exception:
            try:
                is_summary = task.getChildTasks().size() > 0
            except Exception:
                pass

        parent_asta_id = ''
        try:
            parent = task.getParentTask()
            if parent is not None:
                parent_asta_id = str(parent.getID())
        except Exception:
            pass

        status = 'pending'
        try:
            if task.getActualFinish() is not None:
                status = 'completed'
        except Exception:
            pass

        duration_days = 1
        try:
            duration_days = get_duration_days(task.getDuration())
        except Exception:
            pass

        pred_info = pred_map.get(task_id, {})

        activities.append({
            'asta_id': task_id,
            'name': name,
            'start_date': start,
            'end_date': end,
            'duration_days': duration_days,
            'wbs_level': task_id,
            'parent_asta_id': parent_asta_id,
            'is_summary': is_summary,
            'predecessor_asta_id': pred_info.get('pred_id', ''),
            'link_type': pred_info.get('link_type', 'FS'),
            'lag_days': pred_info.get('lag_days', 0),
            'status': status,
        })

    dates = sorted(
        d for a in activities
        for d in [a['start_date'], a['end_date']]
        if d
    )

    return {
        'activities': activities,
        'project_start': dates[0] if dates else None,
        'project_end': dates[-1] if dates else None,
    }


@app.route('/parse', methods=['POST'])
def parse():
    data = request.get_json(force=True)
    file_url = data.get('file_url')
    if not file_url:
        return jsonify({'error': 'file_url is required'}), 400

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pp') as f:
            tmp_path = f.name
        urllib.request.urlretrieve(file_url, tmp_path)
        result = parse_pp_file(tmp_path)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
