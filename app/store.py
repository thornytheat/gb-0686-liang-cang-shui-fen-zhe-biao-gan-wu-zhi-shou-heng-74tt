import os, sqlite3, json
from pathlib import Path
from app.errors import DomainError
from app import ledger

def connection():
    path=Path(os.environ.get("DATA_DIR","data"));path.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path/"runs.sqlite",timeout=15)
    db.row_factory=sqlite3.Row
    ledger.ensure(db)
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,review TEXT NOT NULL DEFAULT '待复核')")
    return db

def save(result,db=None):
    if db is not None:
        save_with(db,result)
        return fetch(result["manifest"]["run_id"])
    with connection() as conn:
        save_with(conn,result)
    return fetch(result["manifest"]["run_id"])

def save_with(db,result):
    db.execute("INSERT OR IGNORE INTO runs(id,payload) VALUES(?,?)",(result["manifest"]["run_id"],json.dumps(result,ensure_ascii=False)))

def refresh_with(db,result):
    """相同输入摘要重算后刷新证据，保留首次创建时间、复核版本与结论。"""
    db.execute("UPDATE runs SET payload=? WHERE id=?",(json.dumps(result,ensure_ascii=False),result["manifest"]["run_id"]))

def existing(run_id,db):
    return db.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone()

def fetch(run_id):
    with connection() as db: row=db.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone()
    if row is None: raise DomainError("NOT_FOUND","批次不存在",404)
    result=json.loads(row["payload"]);result["version"]=row["version"];result["review"]=row["review"]
    for key,default in (("receipts",[]),("pending",[]),("trip_states",{}),("resumed_trips",[])):
        result.setdefault(key,default)
    return result

def history():
    with connection() as db: rows=db.execute("SELECT id,version,review FROM runs ORDER BY rowid DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]

def review(run_id,version,decision):
    if decision not in ("通过","退回"): raise DomainError("REVIEW","请选择通过或退回")
    if not isinstance(version,int): raise DomainError("STALE","版本必须为整数")
    with connection() as db:
        cur=db.execute("UPDATE runs SET review=?,version=version+1 WHERE id=? AND version=?",(decision,run_id,version))
        if cur.rowcount!=1: raise DomainError("STALE","复核版本已更新",409)
    return fetch(run_id)
