"""Asta Powerproject link correction.

MPXJ's Asta reader reports every relation as FINISH_START and returns lag
values in hours. The .pp SQLite database stores each link's true kind in the
LINK table (LINK_KIND: 0=FS, 1=SS, 2=FF, 3=SF) and its lag as an AstaDuration
whose value is in hours. This module reads the LINK table directly and
rewrites each activity's predecessor/successor link lists from the source
table (true kinds, lags converted hours -> days) and injects the LOE/hammock
tasks MPXJ does not surface. Non-Asta files are left unchanged (the queries
simply fail).
"""

import sqlite3
from datetime import date


def correct_asta_links(activities, tmp_path, status_date=None):
    try:
        conn = sqlite3.connect(tmp_path)
        projids = [r[0] for r in conn.execute('SELECT DISTINCT PROJID FROM LINK')]
    except Exception:
        return

    def _utid_map(table, p):
        m = {}
        try:
            for rid, utid in conn.execute(
                    f'SELECT ID, UNIQUE_TASK_ID FROM {table} WHERE PROJID=?', (p,)):
                m[rid] = str(utid or '').strip() or str(rid)
        except Exception:
            pass
        return m

    def _lag_days(raw):
        # AstaDuration blob '<unit, ?, value>' — the value is stored in hours
        try:
            parts = [x for x in str(raw or '').replace('<', '').replace('>', '').split(',') if x != '']
            v = float(parts[2]) if len(parts) >= 3 else 0.0
        except Exception:
            return 0
        d = v / 8.0
        return int(d) if d == int(d) else round(d, 2)

    def _build_pair_map(p):
        task = _utid_map('TASK', p)
        ms = _utid_map('MILESTONE', p)
        exp_utid, exp_bar = {}, {}
        try:
            for rid, utid, bar in conn.execute(
                    'SELECT ID, UNIQUE_TASK_ID, BAR FROM EXPANDED_TASK WHERE PROJID=?', (p,)):
                exp_utid[rid] = str(utid or '').strip() or str(rid)
                exp_bar[rid] = bar
        except Exception:
            pass
        leaves_by_exp, leaves_by_bar = {}, {}
        for tbl in ('TASK', 'MILESTONE'):
            try:
                for rid, utid, sub, bar in conn.execute(
                        f'SELECT ID, UNIQUE_TASK_ID, SUBPROJECT_ID, BAR FROM {tbl} WHERE PROJID=?', (p,)):
                    u = str(utid or '').strip() or str(rid)
                    if sub is not None:
                        leaves_by_exp.setdefault(sub, []).append(u)
                    elif bar is not None:
                        leaves_by_bar.setdefault(bar, []).append(u)
            except Exception:
                pass
        bar_ids = set()
        try:
            bar_ids = {r[0] for r in conn.execute('SELECT ID FROM BAR WHERE PROJID=?', (p,))}
        except Exception:
            pass
        exps_by_bar = {}
        for rid, bar in exp_bar.items():
            if bar is not None:
                exps_by_bar.setdefault(bar, []).append((rid, exp_utid[rid]))
        tc = {}
        try:
            for rid, tsk in conn.execute(
                    'SELECT ID, TASK FROM task_completed_section WHERE PROJID=?', (p,)):
                tc[rid] = tsk
        except Exception:
            pass

        def resolve(x, depth=0):
            if depth > 6:
                return []
            if x in task:
                return [task[x]]
            if x in ms:
                return [ms[x]]
            if x in bar_ids:
                out = []
                for eid, eutid in exps_by_bar.get(x, []):
                    out.append(eutid)
                    out.extend(leaves_by_exp.get(eid, []))
                out.extend(leaves_by_bar.get(x, []))
                seen, uniq = set(), []
                for u in out:
                    if u not in seen:
                        seen.add(u)
                        uniq.append(u)
                return uniq
            if x in exp_utid:
                return [exp_utid[x]]
            if x in tc:
                return resolve(tc[x], depth + 1)
            return []

        kind_map = {0: 'FS', 1: 'SS', 2: 'FF', 3: 'SF'}
        try:
            link_rows = conn.execute(
                'SELECT START_TASK, END_TASK, LINK_KIND, START_LAG_TIME, END_LAG_TIME, DRIVING, ID '
                'FROM LINK WHERE PROJID=? ORDER BY ID', (p,)).fetchall()
        except Exception:
            return {}
        rows_by_pair = {}
        for st, en, kind, slag, elag, drv, rid in link_rows:
            preds = resolve(st)
            succs = resolve(en)
            if not preds or not succs:
                continue
            try:
                t = kind_map.get(int(kind), 'FS')
            except Exception:
                t = 'FS'
            lag = _lag_days(slag) - _lag_days(elag)
            ambig = len(preds) > 1 or len(succs) > 1
            for pr in preds:
                for su in succs:
                    rows_by_pair.setdefault(f'{pr}>{su}', []).append((t, lag, ambig, 1 if drv else 0, rid))
        # Keep EVERY LINK row per (pred, succ) pair, ordered: a .pp can define
        # several links for the same pair (e.g. an FS with a -1-day end lag
        # next to a plain FS) and the XER export carries all of them — the
        # importer zips them onto the parsed link entries pair by pair.
        best = {}
        for k, rows in rows_by_pair.items():
            best[k] = sorted(rows, key=lambda r: (r[2], -r[3], r[4]))

        # Constraints: MPXJ's Asta reader does not expose Powerproject
        # constraints. Read them straight from the source tables and resolve
        # to leaf UTIDs. Flag meanings verified against the XER Toolkit
        # reference output for this programme: 1='Start On',
        # 3='Must Start on or After'; other values are best-effort.
        flag_type = {
            1: 'Start On', 2: 'Start On or Before', 3: 'Must Start on or After',
            4: 'Finish On', 5: 'Finish On or Before', 6: 'Must Finish on or After',
            7: 'As Late As Possible', 8: 'As Early As Possible',
        }
        con = {}
        for tbl in ('TASK', 'MILESTONE'):
            try:
                rows = conn.execute(
                    f'SELECT ID, UNIQUE_TASK_ID, CONSTRAINT_FLAG, START_CONSTRAINT_DATE, END_CONSTRAINT_DATE '
                    f'FROM {tbl} WHERE PROJID=? '
                    f'AND CONSTRAINT_FLAG IS NOT NULL AND CONSTRAINT_FLAG != 0',
                    (p,)).fetchall()
            except Exception:
                continue
            for rid, utid, flag, sd, ed in rows:
                try:
                    t = flag_type.get(int(flag))
                except Exception:
                    t = None
                if not t:
                    continue
                u = str(utid or '').strip() or str(rid)
                con[u] = (t, str(sd or ed or '')[:10])

        # As-Late-As-Possible placement: Asta stores 'as late as possible' as
        # PLACEMENT=1 rather than a constraint flag — a P6/XER export carries
        # it as an explicit As-Late-As-Possible constraint. Constraint flags
        # win when both are set (verified against the XER Toolkit reference
        # output for this programme).
        for tbl in ('TASK', 'MILESTONE'):
            try:
                rows = conn.execute(
                    f'SELECT ID, UNIQUE_TASK_ID FROM {tbl} WHERE PROJID=? '
                    f'AND PLACEMENT = 1', (p,)).fetchall()
            except Exception:
                continue
            for rid, utid in rows:
                u = str(utid or '').strip() or str(rid)
                if u not in con:
                    con[u] = ('As Late As Possible', '')
        # Tasks pinned beyond their logic: Asta allows tasks to be freely
        # positioned, but a P6/XER export can only reproduce that as a
        # 'Must Start on or After' constraint — the XER Toolkit reference
        # output contains exactly these synthetic constraints for tasks
        # whose scheduled start sits after their link-driven start.
        for tbl in ('TASK', 'MILESTONE'):
            try:
                rows = conn.execute(
                    f'SELECT ID, UNIQUE_TASK_ID, EARLY_START_DATE, LINKABLE_START '
                    f'FROM {tbl} WHERE PROJID=? AND CONSTRAINT_FLAG = 0 '
                    f'AND EARLY_START_DATE IS NOT NULL AND LINKABLE_START IS NOT NULL '
                    f'AND EARLY_START_DATE > LINKABLE_START', (p,)).fetchall()
            except Exception:
                continue
            for rid, utid, es, ls in rows:
                u = str(utid or '').strip() or str(rid)
                if u not in con:
                    con[u] = ('Must Start on or After', str(es or '')[:10])
        return best, con

    def _hits(pair_map):
        n = 0
        for a in activities:
            au = a.get('asta_id')
            for pr in a.get('all_predecessors') or []:
                key = f"{pr.get('pred_unique_id') or pr.get('pred_id')}>{au}"
                if key in pair_map:
                    n += 1
        return n

    chosen, chosen_hits, chosen_con, chosen_p = None, 0, {}, None
    for p in projids:
        m, con_map = _build_pair_map(p)
        h = _hits(m)
        if h > chosen_hits:
            chosen, chosen_hits, chosen_con, chosen_p = m, h, con_map, p

    # Grouping containers (Asta expanded tasks): a LEAF activity whose UTID
    # belongs to an expanded task is an empty grouping node (no tasks of its
    # own) — MPXJ surfaces it as a stray leaf task. A P6 XER import keeps
    # every container as a WBS element even when it holds no activities, so
    # keep them too — flagged as summaries so they stay out of the scored
    # task population but still count as WBS nodes (and give their parent
    # container a WBS group) in the integrity engine. Containers WITH
    # members already arrive flagged as summaries. Run BEFORE the hammock
    # injection so injected LOE tasks are never touched.
    exp_utids = set()
    try:
        for (utid,) in conn.execute(
                'SELECT UNIQUE_TASK_ID FROM EXPANDED_TASK WHERE PROJID=?', (chosen_p or 0,)):
            u = str(utid or '').strip()
            if u:
                exp_utids.add(u)
    except Exception:
        exp_utids = set()
    if exp_utids:
        for a in activities:
            if (not a.get('is_summary')
                    and str(a.get('asta_id') or '').strip() in exp_utids):
                a['is_summary'] = True

    # LOE / hammock tasks: MPXJ does not read the HAMMOCK_TASK table, but a
    # P6 import of the same file carries them as regular activities.
    injected = _inject_hammocks(conn, chosen_p or 0, status_date)
    activities.extend(injected)

    # Row-id → UTID lookups (TASK/MILESTONE) used to resolve allocation and
    # hammock-member endpoints in the sections below.
    row_utid = {}
    for tbl in ('TASK', 'MILESTONE'):
        try:
            for rid, utid in conn.execute(
                    f'SELECT ID, UNIQUE_TASK_ID FROM {tbl} WHERE PROJID=?',
                    (chosen_p or 0,)):
                row_utid[rid] = str(utid or '').strip() or str(rid)
        except Exception:
            pass
    by_au = {str(a.get('asta_id') or '').strip(): a for a in activities}

    # ── Resource allocations ─────────────────────────────────────────────
    # MPXJ's Asta reader does not surface consumable allocations at all, and
    # a P6/XER export carries every resource assignment with its scheduled
    # allocation dates. Read the allocation tables directly: consumables
    # become temporary (consumed-in-quantity) assignments on their task with
    # their LINKABLE_START/FINISH dates; permanent allocations contribute
    # their scheduled dates to the matching assignment (matched by resource
    # name where the skill → resource chain resolves).
    try:
        cons_names = dict(conn.execute(
            'SELECT ID, NAME FROM CONSUMABLE_RESOURCE WHERE PROJID=?',
            (chosen_p or 0,)).fetchall())
        csa_rows = conn.execute(
            'SELECT ALLOCATED_TO, ALLOCATION_OF, QUANTITY, LINKABLE_START, LINKABLE_FINISH '
            'FROM CONSUMABLE_SCHEDU_ALLOCATION WHERE PROJID=?',
            (chosen_p or 0,)).fetchall()
    except Exception:
        cons_names, csa_rows = {}, []
    try:
        perm_names = dict(conn.execute(
            'SELECT ID, NAME FROM PERMANENT_RESOURCE WHERE PROJID=?',
            (chosen_p or 0,)).fetchall())
        skill_names = {}
        for sid, app in conn.execute(
                'SELECT ID, SKILL_APPEARANCE FROM PERM_RESOURCE_SKILL WHERE PROJID=?',
                (chosen_p or 0,)).fetchall():
            skill_names[sid] = perm_names.get(app) or ''
        psa_rows = conn.execute(
            'SELECT ALLOCATED_TO, ALLOCATION_OF, LINKABLE_START, LINKABLE_FINISH '
            'FROM PERMANENT_SCHEDUL_ALLOCATION WHERE PROJID=?',
            (chosen_p or 0,)).fetchall()
    except Exception:
        skill_names, psa_rows = {}, []
    for at, aof, qty, ls, lf in csa_rows:
        a = by_au.get(row_utid.get(at) or '')
        if not a:
            continue
        a['resource_assignments'] = a.get('resource_assignments') or []
        a['resource_assignments'].append({
            'name': cons_names.get(aof) or 'Consumable',
            'resource_type': 'temporary',
            'category': 'plant',
            'units': qty or 1,
            'start': str(ls or '')[:10] or None,
            'finish': str(lf or '')[:10] or None,
        })
    for at, aof, ls, lf in psa_rows:
        a = by_au.get(row_utid.get(at) or '')
        nm = skill_names.get(aof) or ''
        if not a or not nm:
            continue
        for ra in a.get('resource_assignments') or []:
            if ra.get('name') == nm and not ra.get('start'):
                ra['start'] = str(ls or '')[:10] or None
                ra['finish'] = str(lf or '')[:10] or None

    # Hammock member pairs (hammock activity, member UTID) — wired into the
    # links below once the rebuild has run.
    hm_rowid = {}
    for a in injected:
        try:
            hm_rowid[int(str(a.get('mpxj_unique_id') or '')[2:])] = a
        except (ValueError, TypeError):
            pass
    member_pairs = []
    if hm_rowid:
        try:
            for hm_rid, mem_rid in conn.execute(
                    'SELECT MEMBERS, SUMMARISED_BY FROM HAMMOCK_TASK_MEMBERS WHERE PROJID=?',
                    (chosen_p or 0,)).fetchall():
                ha = hm_rowid.get(hm_rid)
                mu = row_utid.get(mem_rid)
                if ha and mu:
                    member_pairs.append((ha, mu))
        except Exception:
            member_pairs = []

    try:
        conn.close()
    except Exception:
        pass
    if not chosen:
        return

    # ── Full link rebuild ─────────────────────────────────────────────────
    # MPXJ reports every Asta relation as Finish-to-Start and silently drops
    # links whose endpoints it never surfaced. Rebuild every activity's
    # predecessor/successor lists from the source LINK table — one entry per
    # LINK row, with the true kind and lag — for every pair whose endpoints
    # are both in the parsed task list. The previous pair-by-pair zip onto
    # MPXJ's relation list duplicated rows whenever the two lists drifted,
    # corrupting the SS/FF mix, so the source table is authoritative. MPXJ
    # relations for pairs the source does not hold are kept as a fallback.
    pop = set()
    for a in activities:
        au = str(a.get('asta_id') or '').strip()
        if au:
            pop.add(au)
    preds_of, succs_of = {}, {}
    for key, rows in chosen.items():
        pr, _, su = key.partition('>')
        if pr not in pop or su not in pop:
            continue
        for t, lag, _ambig, _drv, _rid in rows:
            preds_of.setdefault(su, []).append(
                {'pred_id': pr, 'pred_unique_id': pr, 'link_type': t, 'lag_days': lag})
            succs_of.setdefault(pr, []).append(
                {'succ_unique_id': su, 'succ_id': su, 'link_type': t, 'lag_days': lag})
    pred_pairs = {(e['pred_id'], su) for su, lst in preds_of.items() for e in lst}
    succ_pairs = {(pr, e['succ_unique_id']) for pr, lst in succs_of.items() for e in lst}

    for a in activities:
        au = str(a.get('asta_id') or '').strip()
        rebuilt_preds = preds_of.get(au) or []
        kept_preds = [p for p in (a.get('all_predecessors') or [])
                      if (str(p.get('pred_unique_id') or p.get('pred_id') or '').strip(), au)
                      not in pred_pairs]
        a['all_predecessors'] = rebuilt_preds + kept_preds
        rebuilt_succs = succs_of.get(au) or []
        kept_succs = [s for s in (a.get('all_successors') or [])
                      if (au, str(s.get('succ_unique_id') or s.get('succ_id') or '').strip())
                      not in succ_pairs]
        a['all_successors'] = rebuilt_succs + kept_succs
        if a['all_predecessors']:
            fp = a['all_predecessors'][0]
            a['predecessor_asta_id'] = fp.get('pred_id', '')
            a['link_type'] = fp.get('link_type', 'FS')
            a['lag_days'] = fp.get('lag_days', 0)
        if a['all_successors']:
            a['successor_asta_id'] = a['all_successors'][0].get('succ_unique_id', '')
        c = chosen_con.get(au)
        if c:
            a['constraint_type'] = c[0]
            a['constraint_date'] = c[1]

    # ── LOE driving links ─────────────────────────────────────────────────
    # An Asta hammock's span follows the tasks it covers: it starts when its
    # first member starts and finishes when its last member finishes. Wire
    # each injected LOE to the members in its HAMMOCK_TASK_MEMBERS cover set
    # — the relationships a P6 export of the same file carries (SS from the
    # hammock into every member, FF from every member back into the hammock)
    # — and sit the hammock in the same WBS parent as its first member. For
    # hammocks whose cover set cannot be resolved, fall back to the tasks
    # coinciding with the span boundaries.
    hm_ids = {str(a.get('asta_id') or '').strip() for a in injected}
    wired = set()
    for ha, mu in member_pairs:
        m = by_au.get(mu)
        if not m:
            continue
        au = str(ha.get('asta_id') or '').strip()
        m['all_predecessors'] = m.get('all_predecessors') or []
        m['all_predecessors'].append(
            {'pred_id': au, 'pred_unique_id': au, 'link_type': 'SS', 'lag_days': 0})
        m['all_successors'] = m.get('all_successors') or []
        m['all_successors'].append(
            {'succ_unique_id': au, 'succ_id': au, 'link_type': 'SS', 'lag_days': 0})
        m['all_successors'].append(
            {'succ_unique_id': au, 'succ_id': au, 'link_type': 'FF', 'lag_days': 0})
        ha['all_predecessors'] = ha.get('all_predecessors') or []
        ha['all_predecessors'].append(
            {'pred_id': mu, 'pred_unique_id': mu, 'link_type': 'FF', 'lag_days': 0})
        ha['all_successors'] = ha.get('all_successors') or []
        ha['all_successors'].append(
            {'succ_unique_id': mu, 'succ_id': mu, 'link_type': 'SS', 'lag_days': 0})
        if not m.get('successor_asta_id'):
            m['successor_asta_id'] = au
        if m.get('parent_asta_id') and not ha.get('parent_asta_id'):
            ha['parent_asta_id'] = m['parent_asta_id']
        wired.add(au)
    if hm_ids:
        pool = [a for a in activities
                if not a.get('is_summary') and not a.get('is_milestone')
                and str(a.get('asta_id') or '').strip() not in hm_ids
                and a.get('early_start') and a.get('early_finish')]
        for a in activities:
            au = str(a.get('asta_id') or '').strip()
            if au not in hm_ids or au in wired:
                continue
            members = [m for m in pool
                       if m['early_start'] >= a['early_start']
                       and m['early_finish'] <= a['early_finish']
                       and (m['early_start'] == a['early_start']
                            or m['early_finish'] == a['early_finish'])]
            if not members:
                continue
            first = min(members, key=lambda m: m['early_start'])
            last = max(members, key=lambda m: m['early_finish'])
            for m, t in ((first, 'SS'), (last, 'FF')):
                a['all_predecessors'] = a.get('all_predecessors') or []
                a['all_predecessors'].append(
                    {'pred_id': m['asta_id'], 'pred_unique_id': m['asta_id'],
                     'link_type': t, 'lag_days': 0})
                m['all_successors'] = m.get('all_successors') or []
                m['all_successors'].append(
                    {'succ_unique_id': a['asta_id'], 'succ_id': a['asta_id'],
                     'link_type': t, 'lag_days': 0})
                if not m.get('successor_asta_id'):
                    m['successor_asta_id'] = a['asta_id']
            if a['all_predecessors']:
                a['predecessor_asta_id'] = a['all_predecessors'][0]['pred_id']
                a['link_type'] = a['all_predecessors'][0]['link_type']
                a['lag_days'] = a['all_predecessors'][0]['lag_days']


def _inject_hammocks(conn, p, status_date=None):
    """Asta hammock (level-of-effort) tasks live in their own HAMMOCK_TASK
    table, which MPXJ's reader does not surface — a P6 import of the same
    file carries them as regular activities. Read them from the source
    database and inject them as leaf activities with the current (early)
    schedule dates; their parent is the expanded-task container they sit
    under. Returns [] for files without the table (non-Asta sources)."""
    try:
        rows = conn.execute(
            'SELECT ID, UNIQUE_TASK_ID, NAME, EARLY_START_DATE, EARLY_END_DATE_RS, '
            'LATE_START_DATE, LATE_END_DATE_RS, USER_PERCENT_COMPLETE, SUBPROJECT_ID '
            'FROM HAMMOCK_TASK WHERE PROJID=?', (p,)).fetchall()
    except Exception:
        return []
    if not rows:
        return []
    parent = {}
    try:
        for rid, utid in conn.execute(
                'SELECT ID, UNIQUE_TASK_ID FROM EXPANDED_TASK WHERE PROJID=?', (p,)):
            parent[rid] = str(utid or '').strip()
    except Exception:
        pass
    out = []
    for rid, utid, name, es, ef, ls, lf, pct, sub in rows:
        au = str(utid or '').strip()
        s, e = str(es or '')[:10], str(ef or '')[:10]
        if not au or not s or not e:
            continue
        sd, ed = str(es)[:19].replace(' ', 'T'), str(ef)[:19].replace(' ', 'T')
        ls_s = str(ls or '')[:10] or None
        lf_s = str(lf or '')[:10] or None
        try:
            dur = (date.fromisoformat(e) - date.fromisoformat(s)).days + 1
        except Exception:
            dur = 0
        out.append({
            'asta_id': au,
            'mpxj_unique_id': f'HM{rid}',
            'name': str(name or au),
            'start_date': s,
            'end_date': e,
            'actual_start': None,
            'actual_finish': None,
            'early_start': s,
            'early_finish': e,
            'late_start': ls_s,
            'late_finish': lf_s,
            'baseline_start': None,
            'baseline_finish': None,
            'start_datetime': sd,
            'end_datetime': ed,
            'actual_start_datetime': None,
            'actual_finish_datetime': None,
            'early_start_datetime': sd,
            'early_finish_datetime': ed,
            'late_start_datetime': str(ls)[:19].replace(' ', 'T') if ls else None,
            'late_finish_datetime': str(lf)[:19].replace(' ', 'T') if lf else None,
            'baseline_start_datetime': None,
            'baseline_finish_datetime': None,
            'duration_days': dur,
            'duration_type': 'working',
            'actual_duration_days': 0,
            'remaining_duration_days': dur,
            'percentage_complete': pct or 0,
            # A hammock whose span has opened before the status date reads as
            # in progress in the P6/XER export (no actual dates — its early
            # dates before the progress date are what the toolkit scores).
            'status': 'in_progress' if (status_date and s <= str(status_date)[:10]) else 'pending',
            'wbs_level': '',
            'parent_asta_id': parent.get(sub) or '',
            'is_summary': False,
            'is_milestone': False,
            'is_critical': False,
            'predecessor_asta_id': '',
            'link_type': 'FS',
            'lag_days': 0,
            'all_predecessors': [],
            'priority': 0,
            'cost': 0,
            'calendar_name': '',
            'calendar_working_days': [],
            'calendar_is_24_7': False,
            'notes': '',
            'constraint_type': '',
            'constraint_date': '',
            'resources': [],
            'resource_assignments': [],
            'activity_codes': {},
            'all_successors': [],
            'successor_asta_id': '',
            'total_float_days': None,
            'free_float_days': None,
            'task_type': 'Level Of Effort',
            'wbs_code': '',
            'actual_cost': 0,
            'remaining_cost': 0,
            'baseline_cost': 0,
        })
    return out
