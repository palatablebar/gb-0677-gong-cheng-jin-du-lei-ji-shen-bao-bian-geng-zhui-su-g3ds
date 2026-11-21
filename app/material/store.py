"""甲供材台账持久化：事件幂等入账、签证原子发布与版本保护。

关键保证：
- material_events.event_id 唯一：同一领用/退库重试只入账一次，绝不重复抵扣。
- 发布签证在单事务内完成重算、快照写入、周期关闭；任何一步失败整体回滚，
  旧版签证原样保留（签证发布失败仍保留旧版）。
- 签证版本号条件更新：并发/陈旧提交返回 409，不覆盖新版本。
"""
import json
import sqlite3

from app.config import DATA_DIR
from app.errors import DomainError
from app.material.rules import adjudicate, validate

DB_NAME = "materials.db"


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / DB_NAME, timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute(
        """CREATE TABLE IF NOT EXISTS material_events(
               event_id TEXT PRIMARY KEY,
               material_code TEXT NOT NULL,
               period TEXT NOT NULL,
               payload TEXT NOT NULL,
               batch_id TEXT NOT NULL,
               created_at TEXT NOT NULL DEFAULT (datetime('now'))
           )""")
    db.execute(
        """CREATE TABLE IF NOT EXISTS material_certificates(
               cert_id TEXT PRIMARY KEY,
               period TEXT NOT NULL,
               version INTEGER NOT NULL,
               status TEXT NOT NULL DEFAULT '待签认',
               base_version INTEGER NOT NULL,
               snapshot TEXT NOT NULL,
               digest TEXT NOT NULL,
               created_at TEXT NOT NULL DEFAULT (datetime('now'))
           )""")
    return db


def load_events():
    with connect() as db:
        rows = db.execute(
            "SELECT payload FROM material_events ORDER BY period,event_id").fetchall()
    return [json.loads(r[0]) for r in rows]


def closed_periods():
    with connect() as db:
        rows = db.execute(
            "SELECT DISTINCT period FROM material_certificates WHERE status='已签认'").fetchall()
    return {r[0] for r in rows}


def ingest(events, batch_id, context=None):
    """幂等入账：已存在的 event_id 跳过（重试不重复），新事件校验后落库。

    已关闭周期的事件被规则隔离，不允许改写历史签证。
    整个入账在一个事务内：校验失败不落任何脏数据。
    """
    closed = closed_periods()
    submitted = [e for e in events if isinstance(e, dict)]

    # 第一步：批次内去重，每个 event_id 只保留第一条（重复提交不重复抵扣）。
    deduped, in_batch_duplicates, seen_in_batch = [], set(), set()
    for ev in submitted:
        event_id = str(ev.get("event_id", "")).strip()
        if event_id in seen_in_batch:
            in_batch_duplicates.add(event_id)
            continue
        seen_in_batch.add(event_id)
        deduped.append(ev)

    # 第二步：字段级校验。被拒事件（含关闭期、口径非法）一律不落库。
    good, bad = validate(deduped, closed_periods=closed)
    # 已在库内的 event_id 直接计重复跳过，不参与试算（否则与库内同号事件撞键）。
    with connect() as db:
        existing = {r[0] for r in db.execute(
            "SELECT event_id FROM material_events").fetchall()}
    duplicated = len([e for e in good if e["event_id"] in existing])
    good = [e for e in good if e["event_id"] not in existing]
    # 第三步：把新事件与库内权威台账合并试算，
    # 跨事件口径问题（无领用依据耗用、单价冲突）按 event_id 精准挡在库外。
    candidate_ids = {e["event_id"] for e in good}
    trial = adjudicate(load_events() + good, closed_periods=None)
    trial_rejected_ids = candidate_ids & {
        str(q.get("event_id", "")) for q in trial["quarantine"]}
    # 组级隔离会连坐同组事件；对每个候选事件单独复核，只剔除真正违规者，
    # 让同批中合法的配套事件（如被坏耗用拖累的领用）正常入账。
    if trial_rejected_ids:
        stored = load_events()
        truly_bad = set()
        for event_id in trial_rejected_ids:
            ev = next(e for e in good if e["event_id"] == event_id)
            solo = adjudicate(stored + [ev], closed_periods=None)
            if any(str(q.get("event_id", "")) == event_id for q in solo["quarantine"]):
                truly_bad.add(event_id)
        trial_rejected_ids = truly_bad
        for q in trial["quarantine"]:
            if str(q.get("event_id", "")) in trial_rejected_ids:
                bad.append(q)
        good = [e for e in good if e["event_id"] not in trial_rejected_ids]
    rejected_ids = {str(r.get("event_id", "")) for r in bad}
    inserted = []
    # 第四步：仅持久化通过全部校验的新事件。
    # 并发下可能与另一个入账线程撞同一 event_id：用 INSERT OR IGNORE 把竞争落败
    # 计为重复跳过，保证同事件在任何重试/并发下都只抵扣一次。
    with connect() as db:
        for ev in good:
            cursor = db.execute(
                "INSERT OR IGNORE INTO material_events(event_id,material_code,period,payload,batch_id)"
                " VALUES(?,?,?,?,?)",
                (ev["event_id"], ev["material_code"], ev["period"],
                 json.dumps(ev, ensure_ascii=False, default=str), batch_id))
            if cursor.rowcount == 1:
                inserted.append(ev["event_id"])
            else:
                duplicated += 1
    duplicated += len(in_batch_duplicates)
    # 第三步：基于已落库的完整权威台账重算。已落库事件在入账时已通过关闭期校验，
    # 此处不再按关闭期二次隔离，避免把历史签证自身的事件误判为违规。
    settled = adjudicate(load_events(), context=context, closed_periods=None)
    # 本批被拒事件仍随响应返回，供工作台展示待核事项。
    settled["quarantine"] = bad + settled["quarantine"]
    settled["metrics"]["quarantine"] = len(settled["quarantine"])
    settled["metrics"]["balanced"] = not settled["quarantine"]
    settled["ingest"] = {
        "batch_id": batch_id,
        "submitted": len(submitted),
        "inserted": len(inserted),
        "duplicate_skipped": duplicated,
        "rejected": len(bad),
        "rejected_ids": sorted(rejected_ids),
        "closed_periods": sorted(closed),
    }
    return settled


def _cert_digest(snapshot):
    import hashlib
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                         default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def publish_certificate(period, context=None, base_version=None):
    """发布某周期材料抵扣签证（重算与写入在同一事务，失败整体回滚、旧版保留）。

    - 同内容重复发布：幂等返回已存在签证，不产生新版本、不重复关闭周期。
    - base_version 不匹配：409，旧版保留。
    - 存在隔离行：拒绝发布（UNSETTLED_QUARANTINE），旧版保留。
    """
    cert_id = "MC-" + period
    db = connect()
    try:
        # BEGIN IMMEDIATE 立即取写锁：发布期间并发入账会阻塞，保证快照与库内状态一致。
        db.execute("BEGIN IMMEDIATE")
        stored = [json.loads(r[0]) for r in db.execute(
            "SELECT payload FROM material_events ORDER BY period,event_id")]
        settled = adjudicate(stored, context=context, closed_periods=None)
        blocked = [q for q in settled["quarantine"] if str(q.get("period", "")) == period]
        rows = [r for r in settled["rows"] if r["period"] == period]
        if blocked:
            raise DomainError("UNSETTLED_QUARANTINE", "存在隔离事项，签证不能发布")
        if not rows:
            raise DomainError("NO_PERIOD_DATA", "该周期无可签认材料记录", 404)

        snapshot = {"period": period, "rows": rows,
                    "metrics": settled["metrics"], "rule_version": "material-2026.11-r1"}
        digest = _cert_digest(snapshot)
        existing = db.execute(
            "SELECT version,status,digest FROM material_certificates WHERE cert_id=?",
            (cert_id,)).fetchone()
        created = True
        if existing:
            version, _status, old_digest = existing
            # 同内容重发永远幂等：无论是否带基版本，都沿用原签证、不增版本。
            if old_digest == digest:
                db.commit()
                return _cert_payload(db, cert_id, settled, created=False)
            # 内容确有变化：必须携带与当前版本一致的基版本令牌，否则拒绝并保留旧版。
            if base_version is None or int(base_version) != version:
                raise DomainError("STALE_VERSION", "签证版本已变化，请基于最新版重试", 409)
            db.execute(
                "UPDATE material_certificates SET version=version+1,status='待签认',"
                "base_version=?,snapshot=?,digest=? WHERE cert_id=? AND version=?",
                (version, json.dumps(snapshot, ensure_ascii=False), digest,
                 cert_id, version))
        else:
            if base_version not in (None, 0):
                raise DomainError("STALE_VERSION", "签证不存在，基版本应为0", 409)
            db.execute(
                "INSERT INTO material_certificates(cert_id,period,version,status,base_version,"
                "snapshot,digest) VALUES(?,?,1,'待签认',0,?,?)",
                (cert_id, period, json.dumps(snapshot, ensure_ascii=False), digest))
        db.commit()
        return _cert_payload(db, cert_id, settled, created=created)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def sign_certificate(cert_id, version, decision):
    """签认/退回，乐观锁条件更新，陈旧版本 409 且不改写数据。"""
    if decision not in {"已签认", "退回"}:
        raise DomainError("BAD_DECISION", "签认状态无效")
    with connect() as db:
        cursor = db.execute(
            "UPDATE material_certificates SET status=? WHERE cert_id=? AND version=?",
            (decision, cert_id, version))
        if cursor.rowcount != 1:
            if not db.execute("SELECT 1 FROM material_certificates WHERE cert_id=?",
                              (cert_id,)).fetchone():
                raise DomainError("NOT_FOUND", "签证不存在", 404)
            raise DomainError("STALE_VERSION", "版本已更新，请刷新", 409)
    return get_certificate(cert_id)


def _cert_payload(db, cert_id, settled, created):
    row = db.execute(
        "SELECT cert_id,period,version,status,base_version,snapshot,digest,created_at"
        " FROM material_certificates WHERE cert_id=?", (cert_id,)).fetchone()
    return {"cert_id": row[0], "period": row[1], "version": row[2], "status": row[3],
            "base_version": row[4], "snapshot": json.loads(row[5]), "digest": row[6],
            "created_at": row[7], "created": created,
            "ledger_metrics": settled["metrics"]}


def get_certificate(cert_id):
    with connect() as db:
        row = db.execute(
            "SELECT cert_id,period,version,status,base_version,snapshot,digest,created_at"
            " FROM material_certificates WHERE cert_id=?", (cert_id,)).fetchone()
    if not row:
        raise DomainError("NOT_FOUND", "签证不存在", 404)
    return {"cert_id": row[0], "period": row[1], "version": row[2], "status": row[3],
            "base_version": row[4], "snapshot": json.loads(row[5]), "digest": row[6],
            "created_at": row[7]}


def list_certificates():
    with connect() as db:
        rows = db.execute(
            "SELECT cert_id,period,version,status,digest,created_at"
            " FROM material_certificates ORDER BY period DESC, rowid DESC").fetchall()
    return [{"cert_id": r[0], "period": r[1], "version": r[2], "status": r[3],
             "digest": r[4], "created_at": r[5]} for r in rows]


def reset():
    """仅供测试与样例重置。"""
    with connect() as db:
        db.execute("DELETE FROM material_events")
        db.execute("DELETE FROM material_certificates")
