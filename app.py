import os
import tempfile
import traceback
from flask import Flask, request, jsonify
from flask_cors import CORS
from jpype import JClass, JString, startJVM, isThreadAttachedToJVM, attachThreadToJVM

app = Flask(__name__)
CORS(app)

# ── MPXJ JAR setup ──────────────────────────────────────────────────────────
MPXJ_JAR_PATH = os.environ.get('MPXJ_JAR_PATH', '/opt/mpxj/mpxj.jar')

if not isThreadAttachedToJVM():
    startJVM(classpath=MPXJ_JAR_PATH, convertStrings=False)

UniversalProjectReader = JClass('net.sf.mpxj.reader.UniversalProjectReader')
TaskType = JClass('net.sf.mpxj.TaskType')
RelationType = JClass('net.sf.mpxj.RelationType')


def attach_jvm():
    if not isThreadAttachedToJVM():
        attachThreadToJVM()


def to_iso(java_date):
    if java_date is None:
        return None
    try:
        return str(java_date.toString())[:10]
    except:
        return None


def get_task_id(task):
    try:
        tid = task.getID()
        if tid:
            return str(tid)
    except:
        pass
    try:
        uid = task.getUniqueID()
        if uid:
            return str(uid)
    except:
        pass
    return None


def get_wbs_level(task):
    try:
        level = task.getWBSLevel()
        if level:
            return int(str(level))
    except:
        pass
    try:
        outline = task.getOutlineLevel()
        if outline is not None:
            return int(str(outline))
    except:
        pass
    return 1


def get_relation_type_str(rel):
    try:
        rt = rel.getType()
        if rt == RelationType.FINISH_START:
            return 'FS'
        elif rt == RelationType.START_START:
            return 'SS'
        elif rt == RelationType.FINISH_FINISH:
            return 'FF'
        elif rt == RelationType.START_FINISH:
            return 'SF'
    except:
        pass
    return 'FS'


def extract_task_data(task):
    task_id = get_task_id(task)
    if not task_id:
        return None

    start_date = to_iso(task.getStart())
    finish_date = to_iso(task.getFinish())
    planned_start = to_iso(task.getPlannedStart())
    planned_finish = to_iso(task.getPlannedFinish())

    sd = planned_start or start_date
    ed = planned_finish or finish_date

    duration_days = None
    try:
        dur = task.getDuration()
        if dur:
            duration_days = int(str(dur.getDuration()))
    except:
        pass

    wbs_level = get_wbs_level(task)

    is_summary = False
    try:
        if task.getSubprojects() and task.getSubprojects().size() > 0:
            is_summary = True
        tt = task.getTaskType()
        if tt == TaskType.NULL:
            is_summary = True
    except:
        pass

    parent_task_id = None
    try:
        parent = task.getParentTask()
        if parent:
            parent_task_id = get_task_id(parent)
    except:
        pass

    name = ''
    try:
        n = task.getName()
        if n:
            name = str(n)
    except:
        pass

    status = 'pending'
    try:
        pct = task.getPercentageComplete()
        if pct is not None:
            if float(str(pct)) >= 100:
                status = 'completed'
    except:
        pass

    return {
        'asta_id': task_id,
        'name': name,
        'scheduled_date': sd,
        'start_date': sd,
        'end_date': ed,
        'original_date': sd,
        'duration_days': duration_days,
        'wbs_level': wbs_level,
        'is_summary': is_summary,
        'parent_asta_id': parent_task_id,
        'status': status,
    }


def extract_relationships(project):
    relationships = []
    try:
        for task in project.getTasks():
            src_id = get_task_id(task)
            if not src_id:
                continue
            try:
                rels = task.getTaskPredecessors()
                if not rels:
                    continue
                for rel in rels:
                    pred_task = rel.getPredecessorTask()
                    if not pred_task:
                        continue
                    pred_id = get_task_id(pred_task)
                    if not pred_id:
                        continue
                    link_type = get_relation_type_str(rel)
                    lag = 0
                    try:
                        lag_dur = rel.getLag()
                        if lag_dur:
                            lag = int(str(lag_dur.getDuration()))
                    except:
                        pass
                    relationships.append({
                        'predecessor_asta_id': pred_id,
                        'successor_asta_id': src_id,
                        'link_type': link_type,
                        'lag_days': lag,
                    })
            except:
                pass
    except:
        pass
    return relationships


def reconstruct_hierarchy(activities):
    if not activities:
        return activities

    has_parent = sum(1 for a in activities if a.get('parent_asta_id'))
    if has_parent > len(activities) * 0.5:
        return order_depth_first(activities)

    stack = []
    for act in activities:
        level = act.get('wbs_level', 1) or 1
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            act['parent_asta_id'] = stack[-1][1]['asta_id']
        else:
            act['parent_asta_id'] = None
        stack.append((level, act))

    return order_depth_first(activities)


def order_depth_first(activities):
    if not activities:
        return activities

    by_id = {a['asta_id']: a for a in activities if a.get('asta_id')}
    children = {}
    roots = []
    for a in activities:
        pid = a.get('parent_asta_id')
        if pid and pid in by_id:
            children.setdefault(pid, []).append(a)
        else:
            roots.append(a)

    ordered = []
    visited = set()

    def visit(act):
        aid = act.get('asta_id')
        if aid in visited:
            return
        visited.add(aid)
        ordered.append(act)
        for child in children.get(aid, []):
            visit(child)

    for root in roots:
        visit(root)
    for a in activities:
        if a.get('asta_id') not in visited:
            ordered.append(a)

    return ordered


def extract_baselines(project):
    """Extract embedded baselines from the project file."""
    baselines = []
    try:
        baseline_projects = project.getBaselines()
        if not baseline_projects:
            return baselines

        for idx in range(baseline_projects.size()):
            try:
                bl_project = baseline_projects[idx]

                bl_name = f'Baseline {idx + 1}'
                bl_date = None
                try:
                    props = bl_project.getProjectProperties()
                    if props:
                        pn = props.getName()
                        if pn:
                            bl_name = str(pn)
                        sd = props.getStartDate()
                        if sd:
                            bl_date = str(sd.toString())[:10]
                except:
                    pass

                bl_tasks = []
                try:
                    bl_tasks_list = bl_project.getTasks()
                    if bl_tasks_list:
                        for j in range(bl_tasks_list.size()):
                            task = bl_tasks_list[j]
                            task_id = get_task_id(task)
                            if not task_id:
                                continue

                            sd = to_iso(task.getStart())
                            ed = to_iso(task.getFinish())
                            psd = to_iso(task.getPlannedStart())
                            pfd = to_iso(task.getPlannedFinish())
                            sd = psd or sd
                            ed = pfd or ed

                            if sd or ed:
                                bl_tasks.append({
                                    'aid': task_id,
                                    'sd': sd or '',
                                    'ed': ed or '',
                                    'od': sd or '',
                                })
                except:
                    pass

                baselines.append({
                    'name': bl_name,
                    'date': bl_date,
                    'tasks': bl_tasks,
                })

            except Exception as e:
                print(f'Error reading baseline {idx}: {e}')
                continue

    except Exception as e:
        print(f'Error reading baselines: {e}')

    return baselines


@app.route('/parse', methods=['POST'])
def parse_file():
    attach_jvm()

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No filename provided'}), 400

    filename = file.filename
    ext = os.path.splitext(filename)[1].lower()

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        file.save(tmp.name)
        tmp.close()

        try:
            reader = UniversalProjectReader()
            project = reader.read(JString(tmp.name))
        except Exception as e:
            err_msg = str(e).lower()
            if 'password' in err_msg or 'protect' in err_msg:
                return jsonify({
                    'error': 'This file is password-protected. Please remove the password protection in Asta Powerproject and re-upload.'
                }), 422
            return jsonify({'error': f'Failed to parse file: {str(e)}'}), 500

        # Project metadata
        project_start = None
        project_end = None
        try:
            props = project.getProjectProperties()
            if props:
                project_start = to_iso(props.getStartDate())
                project_end = to_iso(props.getFinishDate())
        except:
            pass

        # Extract activities
        activities = []
        all_dates = []

        try:
            tasks = project.getTasks()
            if tasks:
                for i in range(tasks.size()):
                    task = tasks[i]
                    act = extract_task_data(task)
                    if act:
                        activities.append(act)
                        if act['scheduled_date']:
                            all_dates.append(act['scheduled_date'])
                        if act['end_date']:
                            all_dates.append(act['end_date'])
        except Exception as e:
            print(f'Error extracting tasks: {e}')

        # Extract dependencies
        relationships = extract_relationships(project)

        # Apply relationships to activities
        rel_map = {}
        for rel in relationships:
            sid = rel['successor_asta_id']
            if sid not in rel_map:
                rel_map[sid] = rel
        for act in activities:
            rel = rel_map.get(act['asta_id'])
            if rel:
                act['predecessor_asta_id'] = rel['predecessor_asta_id']
                act['link_type'] = rel['link_type']
                act['lag_days'] = rel.get('lag_days', 0)

        # Reconstruct hierarchy and depth-first ordering
        activities = reconstruct_hierarchy(activities)

        # Sort dates for project range
        all_dates = sorted([d for d in all_dates if d])
        if all_dates:
            project_start = project_start or all_dates[0]
            project_end = project_end or all_dates[-1]

        # Extract baselines
        baselines = extract_baselines(project)

        return jsonify({
            'activities': activities,
            'project_start': project_start,
            'project_end': project_end,
            'baselines': baselines,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except:
            pass


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
