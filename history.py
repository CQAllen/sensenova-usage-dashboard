#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SenseNova 用量查询 - 用量历史与耗尽预测

每次成功查询后记录快照到本地 SQLite，提供趋势查询与耗尽预测。
数据仅存本机配置目录，不含凭据。
"""

import os
import sqlite3
import time
from contextlib import closing
from statistics import median

DB_NAME = "usage_history.db"
RETENTION_DAYS = 90
MIN_SNAPSHOTS_FOR_PREDICTION = 3
MIN_SPAN_SECONDS = 15 * 60       # 首末快照至少跨 15 分钟
MAX_RATE_SEGMENTS = 12           # 最多取最近 12 段速率
DEDUP_INTERVAL = 60              # 60 秒内同 key 不重复写入
MAX_CHART_POINTS = 200
PREDICTION_LOOKBACK_DAYS = 7     # 无 reset_at 时最多回看 7 天，避免跨周期


def _db_path(config_dir):
    return os.path.join(config_dir, DB_NAME)


def _connect(config_dir):
    db = sqlite3.connect(_db_path(config_dir), timeout=5)
    db.row_factory = sqlite3.Row
    return db


def init_db(config_dir):
    """建表与索引（幂等）。"""
    os.makedirs(config_dir, exist_ok=True)
    with closing(_connect(config_dir)) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                username TEXT,
                pool_name TEXT,
                pool_type TEXT,
                window TEXT,
                limit_val REAL,
                used_val REAL,
                remain_val REAL,
                reset_at_ts REAL,
                pct REAL
            )
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_snap
            ON snapshots(username, pool_name, window, ts)
        """)
        # 清理按 ts 全表扫描，单独建索引
        db.execute("CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(ts)")
        db.commit()


def record_snapshots(config_dir, accounts_result):
    """从 query_all_accounts() 的返回值中提取快照写入 DB。

    每个账号的每个积分池、每个窗口（5h/7d）各写一条。
    查询失败（有 error）的账号跳过。60 秒内同 key 去重。
    """
    init_db(config_dir)
    now = time.time()

    rows = []
    for acc in accounts_result.get("accounts", []):
        if acc.get("error"):
            continue
        username = acc.get("username", "")
        for pool in acc.get("items", []):
            pool_name = pool.get("pool_name", "")
            pool_type = pool.get("pool_type", "")
            for window in ("5h", "7d"):
                limit_raw = pool.get(f"limit_{window}_raw", 0)
                used_raw = pool.get(f"used_{window}_raw", 0)
                remain_raw = pool.get(f"remain_{window}_raw", 0)
                reset_ts = pool.get(f"reset_at_{window}_ts" if window == "7d" else "reset_at_ts", 0)
                if not limit_raw or limit_raw <= 0:
                    continue  # 上限为 0 不记录
                pct = round(used_raw / limit_raw * 100, 1)
                rows.append((now, username, pool_name, pool_type, window,
                             limit_raw, used_raw, remain_raw, reset_ts or 0, pct))

    if not rows:
        return 0

    inserted = 0
    # 单连接、单事务；先去重再批量写入
    with closing(_connect(config_dir)) as db:
        with db:
            latest = {
                (r["username"], r["pool_name"], r["window"]): r["ts"]
                for r in db.execute(
                    "SELECT username, pool_name, window, MAX(ts) AS ts "
                    "FROM snapshots GROUP BY username, pool_name, window"
                )
            }
            for row in rows:
                key = (row[1], row[2], row[4])
                if key in latest and (now - latest[key]) < DEDUP_INTERVAL:
                    continue
                db.execute(
                    "INSERT INTO snapshots (ts, username, pool_name, pool_type, window, "
                    "limit_val, used_val, remain_val, reset_at_ts, pct) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row
                )
                latest[key] = now
                inserted += 1

    # 顺带清理超期数据（idx_snap_ts 保证删除成本可控）
    try:
        cleanup_old(config_dir)
    except Exception:
        pass
    return inserted


def get_history(config_dir, username=None, pool_name=None, window=None, hours=24):
    """返回趋势数据。按 (username, pool_name, window) 分组，每组返回时间序列点。

    返回: [{"username": str, "pool_name": str, "pool_type": str, "window": str,
            "points": [{"ts": float, "pct": float, "used": float, "remain": float,
                        "reset_at": float}], ...}]
    所有分组共用同一时间范围做时间桶聚合，最多 MAX_CHART_POINTS 个点/组。
    """
    init_db(config_dir)
    cutoff = time.time() - hours * 3600
    query = "SELECT * FROM snapshots WHERE ts >= ?"
    params = [cutoff]
    if username:
        query += " AND username = ?"
        params.append(username)
    if pool_name:
        query += " AND pool_name = ?"
        params.append(pool_name)
    if window:
        query += " AND window = ?"
        params.append(window)
    query += " ORDER BY ts ASC"

    with closing(_connect(config_dir)) as db:
        rows = db.execute(query, params).fetchall()

    if not rows:
        return []

    # 按 (username, pool_name, window) 分组
    groups = {}
    for r in rows:
        key = (r["username"], r["pool_name"], r["window"])
        if key not in groups:
            groups[key] = {"username": r["username"], "pool_name": r["pool_name"],
                           "pool_type": r["pool_type"], "window": r["window"], "points": []}
        groups[key]["points"].append({
            "ts": r["ts"], "pct": r["pct"],
            "used": r["used_val"], "remain": r["remain_val"],
            "reset_at": r["reset_at_ts"] or 0
        })

    start_ts = rows[0]["ts"]
    end_ts = rows[-1]["ts"]

    result = []
    for g in groups.values():
        g["points"] = _bucket_points(g["points"], MAX_CHART_POINTS, start_ts, end_ts)
        result.append(g)
    return result


def _bucket_points(points, max_points, start_ts=None, end_ts=None):
    """数据点过多时按时间桶取最后值；跨重置周期的点不合并到同一桶。"""
    if len(points) <= max_points:
        return points
    t0 = start_ts if start_ts is not None else points[0]["ts"]
    t1 = end_ts if end_ts is not None else points[-1]["ts"]
    span = max(t1 - t0, 1)
    width = span / max_points
    buckets = {}
    for p in points:
        idx = int((p["ts"] - t0) / width)
        # 周期不同则单独成桶，避免曲线跨周期连线时丢失边界点
        buckets[(idx, p["reset_at"])] = p
    ordered = [buckets[k] for k in sorted(buckets)]
    return ordered[-max_points:]


def predict_depletion(config_dir, username, pool_name, window,
                      current_remain, current_reset_at_ts):
    """计算耗尽预测。

    返回 {"rate": float, "prediction": str, "depletion_ts": float|None,
          "status": depleted|insufficient|no_consumption|safe|will_deplete}

    算法（US-03-FR3）：
    1. 取当前周期（相同 reset_at_ts）的快照
    2. 至少 3 条，首末跨度 ≥ 15 分钟
    3. 计算相邻快照 used 增量 / 时间增量 → 速率（积分/小时）
    4. 丢弃负增量（重置）和零时间增量
    5. 取最近最多 12 段速率的中位数
    """
    init_db(config_dir)
    now = time.time()

    if current_remain <= 0:
        reset_str = _ts_to_str(current_reset_at_ts) if current_reset_at_ts else ""
        return {"rate": 0, "status": "depleted",
                "prediction": f"已耗尽{('，' + reset_str + ' 后重置') if reset_str else ''}",
                "depletion_ts": None}

    # 只回看最近一段时间；reset_at 存在时再按周期精确过滤
    cutoff = now - PREDICTION_LOOKBACK_DAYS * 86400
    with closing(_connect(config_dir)) as db:
        rows = db.execute(
            "SELECT ts, used_val, remain_val, reset_at_ts FROM snapshots "
            "WHERE username=? AND pool_name=? AND window=? AND ts>=? ORDER BY ts ASC",
            (username, pool_name, window, cutoff)
        ).fetchall()

    if len(rows) < MIN_SNAPSHOTS_FOR_PREDICTION:
        return {"rate": 0, "status": "insufficient",
                "prediction": "数据不足，继续观察", "depletion_ts": None}

    latest_reset = rows[-1]["reset_at_ts"]
    cycle_rows = [r for r in rows if r["reset_at_ts"] == latest_reset] if latest_reset else rows

    if len(cycle_rows) < MIN_SNAPSHOTS_FOR_PREDICTION:
        return {"rate": 0, "status": "insufficient",
                "prediction": "数据不足，继续观察", "depletion_ts": None}

    span = cycle_rows[-1]["ts"] - cycle_rows[0]["ts"]
    if span < MIN_SPAN_SECONDS:
        return {"rate": 0, "status": "insufficient",
                "prediction": "数据不足，继续观察", "depletion_ts": None}

    rates = []
    for i in range(1, len(cycle_rows)):
        dt = cycle_rows[i]["ts"] - cycle_rows[i - 1]["ts"]
        du = cycle_rows[i]["used_val"] - cycle_rows[i - 1]["used_val"]
        if dt <= 0 or du < 0:
            continue  # 丢弃负增量和零时间
        rates.append(du / dt * 3600)  # 转为积分/小时

    if not rates:
        return {"rate": 0, "status": "no_consumption",
                "prediction": "暂无消耗", "depletion_ts": None}

    rate = median(rates[-MAX_RATE_SEGMENTS:])
    if rate <= 0:
        return {"rate": 0, "status": "no_consumption",
                "prediction": "暂无消耗", "depletion_ts": None}

    hours_left = current_remain / rate
    depletion_ts = now + hours_left * 3600

    if current_reset_at_ts and depletion_ts >= current_reset_at_ts:
        return {"rate": round(rate, 1), "status": "safe",
                "prediction": "重置前不会耗尽", "depletion_ts": None}

    dep_str = _ts_to_str(depletion_ts)
    return {"rate": round(rate, 1), "status": "will_deplete",
            "prediction": f"约 {_fmt_duration(hours_left * 3600)}后耗尽（{dep_str}）",
            "depletion_ts": depletion_ts}


def get_predictions(config_dir, username):
    """返回某账号每个积分池、每个窗口的最新预测。

    返回: [{"pool_name": str, "pool_type": str, "window": str, "as_of_ts": float,
            "rate": float, "prediction": str, "depletion_ts": float|None, "status": str}]
    """
    init_db(config_dir)
    with closing(_connect(config_dir)) as db:
        latest = db.execute(
            "SELECT s.pool_name, s.pool_type, s.window, s.remain_val, s.reset_at_ts, s.ts "
            "FROM snapshots s "
            "WHERE s.username=? AND s.ts = ("
            "  SELECT MAX(ts) FROM snapshots "
            "  WHERE username=s.username AND pool_name=s.pool_name AND window=s.window"
            ") ORDER BY s.pool_name, s.window",
            (username,)
        ).fetchall()

    result = []
    for row in latest:
        pred = predict_depletion(config_dir, username, row["pool_name"], row["window"],
                                 row["remain_val"], row["reset_at_ts"])
        item = {"pool_name": row["pool_name"], "pool_type": row["pool_type"],
                "window": row["window"], "as_of_ts": row["ts"]}
        item.update(pred)
        result.append(item)
    return result


def cleanup_old(config_dir, days=RETENTION_DAYS):
    """删除超期记录。"""
    cutoff = time.time() - days * 86400
    with closing(_connect(config_dir)) as db:
        with db:
            db.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))


def clear_all(config_dir):
    """清除全部历史，并回收文件空间。"""
    with closing(_connect(config_dir)) as db:
        db.execute("DELETE FROM snapshots")
        db.commit()
        try:
            db.execute("VACUUM")
        except sqlite3.Error:
            pass


def get_last_ts(config_dir):
    """返回最近一条快照的时间戳，无数据返回 0。"""
    init_db(config_dir)
    with closing(_connect(config_dir)) as db:
        row = db.execute("SELECT MAX(ts) AS m FROM snapshots").fetchone()
        return row["m"] if row and row["m"] else 0


def _fmt_duration(seconds):
    """秒数转可读时长：2 天 3 小时 / 3 小时 20 分 / 45 分钟。"""
    total_min = max(1, int(round(seconds / 60)))
    days, rem = divmod(total_min, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {mins} 分"
    return f"{mins} 分钟"


def _ts_to_str(ts):
    """时间戳转「HH:MM」或「M月D日 HH:MM」。"""
    if not ts:
        return ""
    t = time.localtime(ts)
    now = time.localtime()
    if t.tm_yday == now.tm_yday and t.tm_year == now.tm_year:
        return time.strftime("%H:%M", t)
    return f"{t.tm_mon}月{t.tm_mday}日 {time.strftime('%H:%M', t)}"
