from importlib import import_module
from time import perf_counter
import json

from app.config import STAGES, DEFAULTS, SAMPLES
from app.manifest import make
from app.normalize import serial
from app.errors import DomainError
from app import store, ledger


def _execute(raw, params, stop, conn):
    actual = {**DEFAULTS, **(params or {})}
    ctx = {"source": raw, "params": actual, "stages": [], "quarantine": []}
    if conn is not None:
        ctx["_conn"] = conn
    for stage in range(1, stop + 1):
        mod = import_module(f"app.modules.m{stage}_{STAGES[stage-1]}.service")
        ctx = mod.execute(ctx)
    return ctx


def _receipt_view(parsed):
    out = []
    for row in parsed:
        view = {
            "_key": row.get("_key", ""),
            "_trip": row.get("_trip", ""),
            "_source": row.get("_source", ""),
            "_version": row.get("_version", 0),
            "_line": row.get("_line", ""),
            "id": row.get("id", ""),
            "trip": row.get("_trip", ""),
            "version": row.get("version", ""),
            "source": row.get("source", row.get("_source", "")),
        }
        view.update(row.get("_raw_fields") or {})
        out.append(view)
    return out


def run(raw=None, params=None, stop=7, persist=True):
    if raw is None:
        raw = (SAMPLES / "dirty.csv").read_bytes()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(params, (dict, type(None))):
        raise DomainError("PARAMS", "参数必须是对象")
    actual = {**DEFAULTS, **(params or {})}
    full = persist and stop == 7
    conn = store.connection() if full else None
    t = perf_counter()
    try:
        recovered = 0
        if conn is not None:
            # 进程中断留下的回执先登记为未完成车次，随后与本轮一起续跑。
            with conn:
                recovered = ledger.recover(conn)
        ctx = _execute(raw, params, stop, conn)
        result = serial({
            **{k: v for k, v in ctx.items() if not k.startswith("_") and k not in ("source", "raw")},
            "receipts": _receipt_view([r for r in ctx.get("_parsed_receipts", [])
                                       if r.get("_origin") == "current"]),
            "pending": ctx.get("_pending", []),
            "trip_states": ctx.get("_outcomes", {}),
            "resumed_trips": ctx.get("_resumed_trips", []),
            "retracted_trips": ctx.get("_retracted", []),
            "inactive_receipts": ctx.get("_inactive", []),
            "recovered_trips": recovered,
        })
        result["manifest"] = make(raw, actual)
        result["elapsed_ms"] = round((perf_counter() - t) * 1000, 3)
        result["version"] = 1
        result["review"] = "待复核"
        if not full:
            return result
        run_id = result["manifest"]["run_id"]
        existing = store.existing(run_id, conn)
        if existing is not None:
            # 相同输入摘要重跑：保留首次创建时间与复核状态，但用台账里的
            # 最新回执刷新事实（补传/复验到达后能看到合并结果）。
            prior = json.loads(existing["payload"])
            result["manifest"]["created"] = prior["manifest"]["created"]
            result["version"] = existing["version"]
            result["review"] = existing["review"]
        payload = {
            "run_id": run_id,
            "receipts": result.get("receipts", []),
            "facts": result.get("records", []),
            "pending": result.get("pending", []),
            "outcomes": result.get("trip_states", {}),
            "retracted": result.get("retracted_trips", []),
            "inactive": result.get("inactive_receipts", []),
        }
        # 第一阶段：先落回执。回执不是事实，页面上看不到半张合并结果。
        with conn:
            ledger.begin(conn, run_id)
            ledger.stage(conn, payload)
        try:
            # 第二阶段：事实切换与批次记录同一事务，要么全部可见，要么全部回滚。
            with conn:
                ledger.switch(conn, payload)
                if existing is None:
                    store.save_with(conn, result)
                else:
                    store.refresh_with(conn, result)
                ledger.finish(conn, run_id)
        except Exception:
            # 切换失败：旧事实保持完整；本次回执标记为未完成，恢复后续跑。
            with conn:
                ledger.mark_failed(conn, run_id)
                ledger.finish(conn, run_id, '切换失败')
            raise
        return store.fetch(run_id)
    finally:
        if conn is not None:
            conn.close()
