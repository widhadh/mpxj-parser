from flask import Flask, request, jsonify
import os, tempfile, urllib.request, traceback
from mpxj.reader import UniversalProjectReader

app = Flask(__name__)

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/parse', methods=['POST'])
def parse():
    data = request.get_json()
    file_url = data.get('file_url')
    if not file_url:
        return jsonify({'error': 'No file_url provided'}), 400

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.pp')
    tmp.close()
    try:
        urllib.request.urlretrieve(file_url, tmp.name)
        project = UniversalProjectReader().read(tmp.name)

        if project is None:
            return jsonify({'error': 'Could not read .pp file — it may be password protected or in an unsupported format.'}), 500

        tasks = project.getTasks()

        # Build task info map
        task_map = {}
        for task in tasks:
            task_id = str(task.getID()) if task.getID() else str(task.getUniqueID())

            wbs_level = ''
            try:
                wl = task.getWbsLevel()
                if wl:
                    wbs_level = str(wl)
            except:
                pass

            start_str = ''
            finish_str = ''
            try:
                start = task.getStart()
                if start:
                    start_str = str(start.toString())[:10]
            except:
                pass
            try:
                finish = task.getFinish()
                if finish:
                    finish_str = str(finish.toString())[:10]
            except:
                pass

            duration_days = 1
            try:
                dur = task.getDuration()
                if dur:
                    duration_days = int(dur)
            except:
                pass

            task_map[id(task)] = {
                'asta_id': task_id,
                'name': str(task.getName() or ''),
                'start_date': start_str or None,
                'end_date': finish_str or None,
                'duration_days': duration_days,
                'wbs_level': wbs_level,
                'parent_asta_id': '',
                'is_summary': False,
                'predecessor_asta_id': '',
                'link_type': 'FS',
                'lag_days': 0,
            }

        # Set parent-child relationships and summary flags
        for task in tasks:
            info = task_map[id(task)]
            try:
                parent = task.getParentTask()
                if parent:
                    parent_info = task_map.get(id(parent))
                    if parent_info:
                        info['parent_asta_id'] = parent_info['asta_id']
            except:
                pass
            try:
                children = task.getChildTasks()
                if children and len(children) > 0:
                    info['is_summary'] = True
            except:
                pass

        # Depth-first ordering
        ordered = []
        visited = set()

        def traverse(task):
            tid = id(task)
            if tid in visited:
                return
            visited.add(tid)
            info = task_map.get(tid)
            if info:
                ordered.append(info)
            try:
                for child in (task.getChildTasks() or []):
                    traverse(child)
            except:
                pass

        top_level = [t for t in tasks if t.getParentTask() is None]
        for task in top_level:
            traverse(task)

        if not ordered:
            for task in tasks:
                info = task_map.get(id(task))
                if info:
                    ordered.append(info)

        # Predecessor relationships
        for task in tasks:
            info = task_map[id(task)]
            try:
                preds = task.getPredecessors()
                if preds:
                    for pred in preds:
                        pred_task = pred.getSourceTask()
                        if pred_task:
                            pred_info = task_map.get(id(pred_task))
                            if pred_info:
                                info['predecessor_asta_id'] = pred_info['asta_id']
                                try:
                                    lt_str = str(pred.getType()).upper()
                                    if 'START_TO_START' in lt_str:
                                        info['link_type'] = 'SS'
                                    elif 'FINISH_TO_FINISH' in lt_str:
                                        info['link_type'] = 'FF'
                                    elif 'START_TO_FINISH' in lt_str:
                                        info['link_type'] = 'SF'
                                    else:
                                        info['link_type'] = 'FS'
                                except:
                                    pass
                                break
            except:
                pass

        all_dates = sorted([d for info in ordered for d in [info['start_date'], info['end_date']] if d])

        # ── Extract embedded baselines (Asta PP files can contain multiple) ──
        baselines = []
        try:
            baseline_projects = project.getBaselines()
            if baseline_projects:
                for idx, bl_project in enumerate(baseline_projects):
                    # Baseline name
                    bl_name = f'Baseline {idx + 1}'
                    try:
                        props = bl_project.getProjectProperties()
                        if props and props.getName():
                            bl_name = str(props.getName())
                    except:
                        pass

                    # Baseline date
                    bl_date = None
                    try:
                        props = bl_project.getProjectProperties()
                        if props and props.getStartDate():
                            bl_date = str(props.getStartDate().toString())[:10]
                    except:
                        pass

                    # Snapshot of task dates
                    bl_tasks = []
                    try:
                        for task in bl_project.getTasks():
                            task_id = str(task.getID()) if task.getID() else str(task.getUniqueID())
                            sd = ''
                            ed = ''
                            try:
                                start = task.getStart()
                                if start:
                                    sd = str(start.toString())[:10]
                            except:
                                pass
                            try:
                                finish = task.getFinish()
                                if finish:
                                    ed = str(finish.toString())[:10]
                            except:
                                pass
                            if task_id and (sd or ed):
                                bl_tasks.append({
                                    'aid': task_id,
                                    'sd': sd,
                                    'ed': ed,
                                    'od': sd,
                                })
                    except:
                        pass

                    baselines.append({
                        'name': bl_name,
                        'date': bl_date,
                        'tasks': bl_tasks,
                    })
        except Exception as e:
            print(f'Error reading baselines: {e}')

        # ── Return response with baselines ──
        return jsonify({
            'activities': ordered,
            'project_start': all_dates[0] if all_dates else None,
            'project_end': all_dates[-1] if all_dates else None,
            'baselines': baselines,
        })
        return jsonify({
            'activities': ordered,
            'project_start': all_dates[0] if all_dates else None,
            'project_end': all_dates[-1] if all_dates else None,
        })

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except:
            pass

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
