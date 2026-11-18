"""收货台账：回执、车次事实与入仓流水。

一次运行的回执入库、事实切换和入仓流水在同一个 SQLite 事务里完成：
任何一步失败都整体回滚，页面不会读到半张事实。车次（trip）是入仓的
唯一身份，事实与入仓流水都以 trip 为主键；同一车次重试只会覆盖，不会
重复入仓。

状态：
- 已入仓：事实完整，已写入 bin_entries；
- 待续跑：合并后仍缺字段，回执已保存，等待补传；
- 冲突 / LIMIT 等：整组隔离，不写事实，也不入仓。
"""
import hashlib
import json
from datetime import datetime, timezone

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS receipts (
        receipt_key TEXT PRIMARY KEY,
        trip TEXT NOT NULL,
        ticket_id TEXT NOT NULL,
        source TEXT NOT NULL,
        version INTEGER NOT NULL,
        line INTEGER,
        payload TEXT NOT NULL,
        run_id TEXT NOT NULL,
        created TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE INDEX IF NOT EXISTS receipts_trip ON receipts(trip)""",
    """CREATE TABLE IF NOT EXISTS trip_facts (
        trip TEXT PRIMARY KEY,
        ticket_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL,
        run_id TEXT NOT NULL,
        updated TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS bin_entries (
        trip TEXT PRIMARY KEY,
        bin TEXT NOT NULL,
        ticket_id TEXT NOT NULL,
        net_kg TEXT NOT NULL,
        dry_kg TEXT NOT NULL,
        standard_kg TEXT NOT NULL,
        run_id TEXT NOT NULL,
        created TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS trip_status (
        trip TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        missing TEXT NOT NULL,
        run_id TEXT NOT NULL,
        updated TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS run_log (
        run_id TEXT PRIMARY KEY,
        started TEXT NOT NULL,
        finished TEXT,
        status TEXT NOT NULL
    )""",
)


def ensure(db):
    for statement in SCHEMA:
        db.execute(statement)
    columns = {row[1] for row in db.execute("PRAGMA table_info(receipts)").fetchall()}
    if 'active' not in columns:
        db.execute("ALTER TABLE receipts ADD COLUMN active INTEGER NOT NULL DEFAULT 1")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def begin(db, run_id):
    """登记一次运行。写事务由调用方统一提交/回滚。"""
    ensure(db)
    db.execute("INSERT INTO run_log(run_id,started,status) VALUES(?,?,?) "
               "ON CONFLICT(run_id) DO UPDATE SET started=excluded.started,status='进行中'",
               (run_id, _now(), '进行中'))


def finish(db, run_id, status='完成'):
    db.execute("UPDATE run_log SET finished=?,status=? WHERE run_id=?",
               (_now(), status, run_id))


def staged_trips(db, run_id):
    ensure(db)
    rows = db.execute("SELECT DISTINCT trip FROM receipts WHERE run_id=?", (run_id,)).fetchall()
    return [row['trip'] for row in rows]


def stage(db, data):
    """第一阶段：只落回执（未切换的事实不可见）。

    回执不是事实，落盘后即使后续切换失败，页面也不会展示半张事实；
    恢复时这些回执可作为“未完成车次”的续跑输入。
    """
    _write_receipts(db, data)


def load_receipts(trips, db):
    """读取指定车次已登记的回执，用于补齐/续跑。"""
    trips = sorted({str(t) for t in trips if str(t)})
    if not trips:
        return []
    ensure(db)
    marks = ','.join('?' for _ in trips)
    rows = db.execute(
        "SELECT * FROM receipts WHERE active=1 AND trip IN (%s) ORDER BY line, receipt_key" % marks,
        trips,
    ).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row['payload'])
        raw = {k: v for k, v in payload.items() if not k.startswith('_')}
        payload = {
            **raw,
            '_line': row['line'] if row['line'] is not None else '',
            '_origin': 'prior',
            '_key': row['receipt_key'],
            '_trip': row['trip'],
            '_source': row['source'],
            '_version': row['version'],
            '_raw_fields': raw,
        }
        out.append(payload)
    return out


def load_identities(trips, db):
    """车次的原磅单身份。

    已入仓车次取事实上的磅单号；尚未完成的车次取台账里最早一张地磅回执
    的磅单号。这样地磅原地重试即使换了回执号，也仍命中原磅单身份。
    """
    trips = sorted({str(t) for t in trips if str(t)})
    if not trips:
        return {}
    ensure(db)
    marks = ','.join('?' for _ in trips)
    out = {}
    receipts = db.execute(
        "SELECT trip,ticket_id,source,line,receipt_key FROM receipts "
        "WHERE trip IN (%s) ORDER BY line,receipt_key" % marks, trips,
    ).fetchall()
    for row in receipts:
        if row['source'] in ('scale', 'full'):
            out.setdefault(row['trip'], row['ticket_id'])
    facts = db.execute(
        "SELECT trip,ticket_id FROM trip_facts WHERE trip IN (%s)" % marks, trips,
    ).fetchall()
    for row in facts:
        out[row['trip']] = row['ticket_id']
    return out


def load_facts(trips, db):
    """读取指定车次已入仓的事实（含原回执与证据）。"""
    trips = sorted({str(t) for t in trips if str(t)})
    if not trips:
        return {}
    ensure(db)
    marks = ','.join('?' for _ in trips)
    rows = db.execute(
        "SELECT * FROM trip_facts WHERE status='已入仓' AND trip IN (%s)" % marks, trips,
    ).fetchall()
    out = {}
    for row in rows:
        payload = json.loads(row['payload'])
        payload['trip'] = row['trip']
        payload['id'] = row['ticket_id']
        payload['ticket_id'] = row['ticket_id']
        payload['version'] = row['version']
        out[row['trip']] = payload
    return out


def load_pending_trips(db):
    ensure(db)
    rows = db.execute("SELECT trip FROM trip_status WHERE status='待续跑' ORDER BY trip").fetchall()
    return [row['trip'] for row in rows]


def _write_receipts(db, data):
    run_id = data.get('run_id', '')
    now = _now()
    inactive = set(data.get('inactive') or [])
    for row in data.get('receipts', []):
        key = row.get('_key') or ''
        if not key:
            continue
        payload = {k: v for k, v in row.items() if not k.startswith('_')}
        # 由毛皮重算出的净重不是原回执字段，不写入回执证据。
        if row.get('gross') is not None and row.get('tare') is not None and row.get('unit') is not None:
            payload.pop('net_kg', None)
        payload['id'] = row.get('id', '')
        payload['trip'] = row.get('trip') or row.get('_trip') or ''
        payload['version'] = str(row.get('version', row.get('_version', '')))
        payload['source'] = row.get('source', row.get('_source', ''))
        db.execute(
            "INSERT INTO receipts(receipt_key,trip,ticket_id,source,version,line,payload,run_id,created,active) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(receipt_key) DO UPDATE SET "
            "payload=excluded.payload, run_id=excluded.run_id, active=excluded.active",
            (key, row.get('_trip') or row.get('trip', ''), str(row.get('id', '')),
             row.get('_source') or row.get('source', ''),
             int(row.get('_version') or row.get('version') or 0),
             row.get('_line') if isinstance(row.get('_line'), int) else None,
             json.dumps(payload, ensure_ascii=False, default=str), run_id, now,
             0 if key in inactive else 1),
        )
    for key in inactive:
        db.execute("UPDATE receipts SET active=0 WHERE receipt_key=?", (key,))


def switch(db, data):
    """第二阶段：切换事实与入仓流水。

    与 store.save_with 处于同一事务；异常时调用方回滚，旧事实保持完整，
    新事实一张也不会露出。
    """
    ensure(db)
    run_id = data.get('run_id', '')
    now = _now()
    for fact in data.get('facts', []):
        trip = fact.get('trip', '')
        payload = {k: v for k, v in fact.items() if not k.startswith('_')}
        payload['id'] = fact.get('id', '')
        payload['trip'] = trip
        payload['receipts'] = fact.get('_receipts', [])
        payload['evidence'] = fact.get('evidence', {})
        db.execute(
            "INSERT INTO trip_facts(trip,ticket_id,digest,payload,status,version,run_id,updated) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(trip) DO UPDATE SET "
            "ticket_id=excluded.ticket_id,digest=excluded.digest,payload=excluded.payload,"
            "status=excluded.status,version=excluded.version,run_id=excluded.run_id,updated=excluded.updated",
            (trip, str(fact.get('id', '')), _digest(payload),
             json.dumps(payload, ensure_ascii=False), '已入仓',
             int(fact.get('version') or 0), run_id, now),
        )
        db.execute(
            "INSERT INTO bin_entries(trip,bin,ticket_id,net_kg,dry_kg,standard_kg,run_id,created) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(trip) DO UPDATE SET "
            "bin=excluded.bin, ticket_id=excluded.ticket_id, net_kg=excluded.net_kg,"
            "dry_kg=excluded.dry_kg, standard_kg=excluded.standard_kg, run_id=excluded.run_id",
            (trip, fact.get('bin', ''), str(fact.get('id', '')),
             str(fact.get('net_kg', '')), str(fact.get('dry_kg', '')),
             str(fact.get('standard_kg', '')), run_id, now),
        )
        db.execute(
            "INSERT INTO trip_status(trip,status,missing,run_id,updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(trip) DO UPDATE SET status=excluded.status, missing=excluded.missing,"
            "run_id=excluded.run_id, updated=excluded.updated",
            (trip, '已入仓', '[]', run_id, now),
        )
    status_names = {'CONFLICT': '冲突', 'LIMIT': '水分超限', 'RANGE': '越界',
                    'DUPLICATE': '重复', 'SOURCE': '来源不符', 'UNMAPPED': '来源未登记',
                    'UNIT': '单位无效', 'NUMBER': '数值错误', 'INCOMPLETE': '不完整',
                    'COLUMN_COUNT': '字段数不符'}
    for item in data.get('pending', []):
        trip = item.get('trip', '')
        if not trip:
            continue
        existing = db.execute("SELECT status FROM trip_status WHERE trip=?", (trip,)).fetchone()
        if existing is not None and existing['status'] == '已入仓':
            continue
        db.execute(
            "INSERT INTO trip_status(trip,status,missing,run_id,updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(trip) DO UPDATE SET status=excluded.status, missing=excluded.missing,"
            "run_id=excluded.run_id, updated=excluded.updated",
            (trip, '待续跑', json.dumps(item.get('missing', []), ensure_ascii=False), run_id, now),
        )
    for trip in data.get('retracted') or []:
        db.execute("DELETE FROM bin_entries WHERE trip=?", (trip,))
        db.execute("UPDATE trip_facts SET status='已退收',updated=? WHERE trip=?", (now, trip))
        db.execute(
            "INSERT INTO trip_status(trip,status,missing,run_id,updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(trip) DO UPDATE SET status=excluded.status, missing=excluded.missing,"
            "run_id=excluded.run_id, updated=excluded.updated",
            (trip, '退收', '[]', run_id, now),
        )
    for trip, outcome in (data.get('outcomes') or {}).items():
        if not trip:
            continue
        status = outcome.get('status', '待续跑')
        existing = db.execute("SELECT status FROM trip_status WHERE trip=?", (trip,)).fetchone()
        if status == '已入仓' or status == 'LIMIT':
            continue
        if existing is not None and existing['status'] in ('已入仓', '退收'):
            # 已有完整事实或已退收的车次不会因为一次坏回执被改写。
            continue
            # 已有完整事实的车次不会因为一次坏回执被撤回。
            continue
        db.execute(
            "INSERT INTO trip_status(trip,status,missing,run_id,updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(trip) DO UPDATE SET status=excluded.status, missing=excluded.missing,"
            "run_id=excluded.run_id, updated=excluded.updated",
            (trip, status_names.get(status, '待续跑'),
             json.dumps(outcome.get('missing', []), ensure_ascii=False), run_id, now),
        )


def mark_failed(db, run_id, outcome='待续跑'):
    """切换失败后把本次已落盘回执的车次标为未完成，续跑时优先处理。"""
    ensure(db)
    for trip in staged_trips(db, run_id):
        row = db.execute("SELECT status FROM trip_status WHERE trip=?", (trip,)).fetchone()
        if row is not None and row['status'] == '已入仓':
            continue
        db.execute(
            "INSERT INTO trip_status(trip,status,missing,run_id,updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(trip) DO UPDATE SET status=excluded.status, missing=excluded.missing,"
            "run_id=excluded.run_id, updated=excluded.updated",
            (trip, outcome, '[]', run_id, _now()),
        )


def recover(db):
    """恢复上次进程中断的运行：回执留作续跑，事实保持原样。

    返回恢复的车次数，便于运行清单注明“未完成车次续跑”的来源。
    """
    ensure(db)
    rows = db.execute("SELECT run_id FROM run_log WHERE status='进行中'").fetchall()
    recovered = 0
    for row in rows:
        trips = staged_trips(db, row['run_id'])
        if trips:
            mark_failed(db, row['run_id'])
        finish(db, row['run_id'], '已恢复')
        recovered += len(trips)
    return recovered


def last_run(db, run_id):
    ensure(db)
    row = db.execute("SELECT * FROM run_log WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row is not None else None


def facts(db):
    ensure(db)
    rows = db.execute("SELECT * FROM trip_facts WHERE status='已入仓' ORDER BY ticket_id,trip").fetchall()
    out = []
    for row in rows:
        payload = json.loads(row['payload'])
        payload['trip'] = row['trip']
        payload['id'] = row['ticket_id']
        payload['state'] = row['status']
        out.append(payload)
    return out


def bin_totals(db, exclude_trips=()):
    """仓批累计。exclude_trips 是本次运行会重算的车次，先排除再并入。"""
    ensure(db)
    exclude = sorted({str(t) for t in exclude_trips if str(t)})
    sql = "SELECT bin, COUNT(*) AS count, net_kg, dry_kg, standard_kg FROM bin_entries"
    params = ()
    if exclude:
        sql += " WHERE trip NOT IN (%s)" % ','.join('?' for _ in exclude)
        params = tuple(exclude)
    sql += " GROUP BY bin ORDER BY bin"
    rows = db.execute(sql, params).fetchall()
    return [{'bin': row['bin'], 'count': row['count'],
             'net_kg': row['net_kg'], 'dry_kg': row['dry_kg'],
             'standard_kg': row['standard_kg']} for row in rows]


def statuses(db):
    ensure(db)
    rows = db.execute("SELECT trip,status,missing FROM trip_status ORDER BY trip").fetchall()
    return [{'trip': row['trip'], 'status': row['status'],
             'missing': json.loads(row['missing'])} for row in rows]


def pending_receipts(db):
    """尚未完成的车次及其已保存回执，用于页面提示续跑。"""
    ensure(db)
    trips = load_pending_trips(db)
    if not trips:
        return []
    marks = ','.join('?' for _ in trips)
    rows = db.execute(
        "SELECT trip,receipt_key,ticket_id,source,version,line FROM receipts "
        "WHERE active=1 AND trip IN (%s) ORDER BY trip,line,receipt_key" % marks, trips,
    ).fetchall()
    out = []
    for row in rows:
        out.append(dict(row))
    return out
