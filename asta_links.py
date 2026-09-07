"""Asta Powerproject link correction.

MPXJ's Asta reader reports every relation as FINISH_START and returns lag
values in hours. The .pp SQLite database stores each link's true kind in the
LINK table (LINK_KIND: 0=FS, 1=SS, 2=FF, 3=SF) and its lag as an AstaDuration
whose value is in hours. This module reads the LINK table directly and
rewrites each activity's predecessor/successor link types and lags (converted
hours -> days). Non-Asta files are left unchanged (the queries simply fail).
"""

import sqlite3


def correct_asta_links(activities, tmp_path):
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
        best = {}
        for k, rows in rows_by_pair.items():
            best[k] = sorted(rows, key=lambda r: (r[2], -r[3], r[4]))[0]

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

    chosen, chosen_hits, chosen_con = None, 0, {}
    for p in projids:
        m, con_map = _build_pair_map(p)
        h = _hits(m)
        if h > chosen_hits:
            chosen, chosen_hits, chosen_con = m, h, con_map
    try:
        conn.close()
    except Exception:
        pass
    if not chosen:
        return

    for a in activities:
        au = a.get('asta_id')
        for pr in a.get('all_predecessors') or []:
            row = chosen.get(f"{pr.get('pred_unique_id') or pr.get('pred_id')}>{au}")
            if row:
                pr['link_type'] = row[0]
                pr['lag_days'] = row[1]
        for su in a.get('all_successors') or []:
            row = chosen.get(f"{au}>{su.get('succ_unique_id')}")
            if row:
                su['link_type'] = row[0]
                su['lag_days'] = row[1]
        preds = a.get('all_predecessors') or []
        if preds:
            a['link_type'] = preds[0].get('link_type', a.get('link_type'))
            a['lag_days'] = preds[0].get('lag_days', a.get('lag_days'))
        c = chosen_con.get(au)
        if c:
            a['constraint_type'] = c[0]
            a['constraint_date'] = c[1]
