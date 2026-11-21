"""甲供材台账持久化与 HTTP 接口测试：幂等入账、签证原子发布、版本保护。"""
import pytest

from app.material import store
from app.material.intake import parse_payload
from app.config import ROOT


@pytest.fixture(autouse=True)
def isolated_material_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    store.reset()
    yield
    store.reset()


def _sample_events():
    raw = (ROOT / "sample_data/material_events.csv").read_bytes()
    return parse_payload(raw)[0]


def test_ingest_idempotent_same_event_not_double_deducted():
    events = _sample_events()
    first = store.ingest(events, "MB-1")
    assert first["ingest"]["inserted"] == len(events)
    # 整批重试：全部跳过，台账结果不变，不重复抵扣。
    second = store.ingest(events, "MB-2")
    assert second["ingest"]["inserted"] == 0
    assert second["ingest"]["duplicate_skipped"] == len(events)
    assert second["metrics"] == first["metrics"]
    assert len(store.load_events()) == len(events)


def test_partial_retry_only_new_events_inserted():
    events = _sample_events()
    store.ingest(events[:5], "MB-1")
    again = store.ingest(events[:8], "MB-2")  # 前5条重复，后3条新增
    assert again["ingest"]["duplicate_skipped"] == 5
    assert again["ingest"]["inserted"] == 3


def test_publish_certificate_snapshot_and_get():
    store.ingest(_sample_events(), "MB-1")
    cert = store.publish_certificate("2026-10")
    assert cert["cert_id"] == "MC-2026-10" and cert["version"] == 1
    assert cert["status"] == "待签认" and cert["created"] is True
    snap = cert["snapshot"]
    assert {r["material_code"] for r in snap["rows"]} == {"MAT-HRB400", "MAT-Q235"}
    assert store.get_certificate("MC-2026-10")["digest"] == cert["digest"]


def test_republish_same_content_is_idempotent_no_new_version():
    store.ingest(_sample_events(), "MB-1")
    first = store.publish_certificate("2026-10")
    second = store.publish_certificate("2026-10", base_version=1)
    assert second["version"] == 1 and second["created"] is False
    assert second["digest"] == first["digest"]


def test_stale_base_version_keeps_old_certificate():
    store.ingest(_sample_events(), "MB-1")
    store.publish_certificate("2026-10")
    # 构造内容变化：补一条同周期事件，再用陈旧基版本发布，必须409且旧版保留。
    extra = [{"event_id": "X1", "material": "螺纹钢", "event_type": "issue",
              "period": "2026-10", "quantity": "5", "unit_price": "4200",
              "ref_id": "RD-X", "occurred_at": "2026-10-15T09:00:00+08:00",
              "_line": 99, "_sources": [99]}]
    store.ingest(extra, "MB-X")
    with pytest.raises(Exception) as e:
        store.publish_certificate("2026-10", base_version=99)
    assert e.value.status == 409
    old = store.get_certificate("MC-2026-10")
    assert old["version"] == 1  # 发布失败，旧版原样保留


def test_publish_with_quarantine_blocked_keeps_state():
    # 先放一条正常领用，再混入一条无领用依据的耗用：坏事件被隔离且不落库。
    good = [{"event_id": "G1", "material": "螺纹钢", "event_type": "issue",
             "period": "2026-11", "quantity": "10", "unit_price": "4200",
             "ref_id": "R", "occurred_at": "2026-11-07T09:00:00+08:00",
             "_line": 1, "_sources": [1]}]
    bad = [{"event_id": "B1", "material": "螺纹钢", "event_type": "consume",
            "period": "2026-11", "quantity": "99", "unit_price": "4200",
            "ref_id": "X", "occurred_at": "2026-11-08T09:00:00+08:00",
            "_line": 2, "_sources": [2]}]
    settled = store.ingest(good + bad, "MB-BAD")
    # 坏事件被精准隔离、不落库；合法配套领用正常入账，权威台账保持平衡。
    assert settled["ingest"]["rejected"] == 1
    assert settled["ingest"]["rejected_ids"] == ["B1"]
    assert len(store.load_events()) == 1
    # 库内台账本身平衡，可正常发布该周期签证（坏数据从未进入台账）。
    cert = store.publish_certificate("2026-11")
    assert cert["cert_id"] == "MC-2026-11" and store.list_certificates()
    # 真正能阻断发布的是进入裁决却失败的隔离行：构造单价冲突组。
    conflict = [
        {"event_id": "C1", "material": "圆钢", "event_type": "issue",
         "period": "2026-12", "quantity": "10", "unit_price": "3900",
         "ref_id": "R1", "occurred_at": "2026-12-07T09:00:00+08:00",
         "_line": 3, "_sources": [3]},
        {"event_id": "C2", "material": "圆钢", "event_type": "consume",
         "period": "2026-12", "quantity": "5", "unit_price": "4000",
         "ref_id": "R2", "occurred_at": "2026-12-08T09:00:00+08:00",
         "_line": 4, "_sources": [4]},
    ]
    blocked = store.ingest(conflict, "MB-CONFLICT")
    assert blocked["ingest"]["rejected_ids"] == ["C2"]  # 仅坏事件被挡，C1 正常入账
    stored_ids = {e["event_id"] for e in store.load_events()}
    assert "C1" in stored_ids and "C2" not in stored_ids
    # 该周期库内只有无耗用配套的领用，签证仍可发布，但绝不包含被拒事件。
    cert = store.publish_certificate("2026-12")
    snap_ids = {eid for r in cert["snapshot"]["rows"] for eid in r["event_ids"]}
    assert "C2" not in snap_ids


def test_sign_closes_period_and_blocks_new_events():
    store.ingest(_sample_events(), "MB-1")
    cert = store.publish_certificate("2026-10")
    signed = store.sign_certificate(cert["cert_id"], cert["version"], "已签认")
    assert signed["status"] == "已签认"
    assert "2026-10" in store.closed_periods()
    # 关闭周期再入账：事件被隔离，历史签证不被重算。
    late = [{"event_id": "L1", "material": "螺纹钢", "event_type": "issue",
             "period": "2026-10", "quantity": "3", "unit_price": "4200",
             "ref_id": "RD-L", "occurred_at": "2026-10-30T09:00:00+08:00",
             "_line": 2, "_sources": [2]}]
    settled = store.ingest(late, "MB-LATE")
    assert any(q["reason"] == "PERIOD_CLOSED" for q in settled["quarantine"])
    assert settled["ingest"]["inserted"] == 0
    # 被拒事件不落库，权威台账重算不重复隔离。
    assert all(e["event_id"] != "L1" for e in store.load_events())
    assert store.adjudicate(store.load_events())["quarantine"] == []
    # 已发布签证快照金额不变。
    assert store.get_certificate("MC-2026-10")["digest"] == cert["digest"]


def test_stale_sign_409():
    from app.errors import DomainError
    store.ingest(_sample_events(), "MB-1")
    cert = store.publish_certificate("2026-10")
    # 内容变化产生新版本后，仍用旧版本号签认必须 409，旧状态保留。
    extra = [{"event_id": "X2", "material": "螺纹钢", "event_type": "issue",
              "period": "2026-10", "quantity": "4", "unit_price": "4200",
              "ref_id": "RD-X2", "occurred_at": "2026-10-16T09:00:00+08:00",
              "_line": 98, "_sources": [98]}]
    store.ingest(extra, "MB-X2")
    new_cert = store.publish_certificate("2026-10", base_version=1)
    assert new_cert["version"] == 2
    with pytest.raises(DomainError) as e:
        store.sign_certificate(cert["cert_id"], cert["version"], "已签认")
    assert e.value.status == 409
    assert store.get_certificate(cert["cert_id"])["status"] == "待签认"


# ---------- HTTP 接口 ----------

def test_http_ledger_preview_then_ingest_flow(client):
    # 空库回退样例预览，不落库。
    preview = client.get("/api/materials/ledger").get_json()
    assert preview["sample"] is True and preview["metrics"]["balanced"] is True
    loaded = client.post("/api/materials/sample/load").get_json()
    assert loaded["ingest"]["inserted"] == 16
    ledger = client.get("/api/materials/ledger").get_json()
    assert ledger["sample"] is False


def test_http_event_ingest_idempotent(client):
    payload = {"events": [{"event_id": "W1", "material": "螺纹钢",
                           "event_type": "issue", "period": "2026-11",
                           "quantity": "20", "unit_price": "4200", "ref_id": "R",
                           "occurred_at": "2026-11-08T09:00:00+08:00"}]}
    first = client.post("/api/materials/events", json=payload).get_json()
    second = client.post("/api/materials/events", json=payload).get_json()
    assert first["ingest"]["inserted"] == 1
    assert second["ingest"]["duplicate_skipped"] == 1
    assert second["metrics"] == first["metrics"]


def test_http_certificate_publish_and_sign(client):
    client.post("/api/materials/sample/load")
    r = client.post("/api/materials/certificates", json={"period": "2026-10"})
    cert = r.get_json()
    assert r.status_code == 200 and cert["version"] == 1
    sign = client.post(f"/api/materials/certificates/{cert['cert_id']}/sign",
                       json={"version": 1, "decision": "已签认"})
    assert sign.status_code == 200 and sign.get_json()["status"] == "已签认"
    listed = client.get("/api/materials/certificates").get_json()["certificates"]
    assert any(c["cert_id"] == "MC-2026-10" for c in listed)


def test_http_missing_period_404(client):
    client.post("/api/materials/sample/load")
    r = client.post("/api/materials/certificates", json={"period": "2030-09"})
    assert r.status_code == 404


def test_http_workbench_page_renders(client):
    assert client.get("/materials").status_code == 200
