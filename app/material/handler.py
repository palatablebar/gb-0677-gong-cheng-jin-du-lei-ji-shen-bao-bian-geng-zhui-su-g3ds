"""甲供钢材跨期承接工作台 HTTP 接口。"""
import uuid

from flask import Blueprint, jsonify, request

from app.errors import DomainError
from app.values import scalar
from app.material import store
from app.material.intake import parse_payload
from app.material.rules import adjudicate

bp = Blueprint("materials", __name__)

SAMPLE_PATH = "sample_data/material_events.csv"


def _sample_bytes():
    from app.config import ROOT
    return (ROOT / SAMPLE_PATH).read_bytes()


@bp.get("/api/materials/ledger")
def ledger():
    """工作台主数据：库内完整台账的滚动承接结果。空库时回退样例预览（不落库）。"""
    from app.config import ROOT
    events = store.load_events()
    if not events:
        raw = (ROOT / SAMPLE_PATH).read_bytes()
        parsed, _ = parse_payload(raw)
        result = adjudicate(parsed)
        result["sample"] = True
        return jsonify(scalar(result))
    result = adjudicate(events, closed_periods=None)
    result["sample"] = False
    result["certificates"] = store.list_certificates()
    return jsonify(scalar(result))


@bp.post("/api/materials/events")
def post_events():
    """事件入账（领用/耗用/退库/余料）。重试同 event_id 幂等跳过，不重复抵扣。"""
    payload = request.get_json(silent=True)
    if payload is None or not isinstance(payload, dict):
        raise DomainError("BAD_BODY", "请求需要对象")
    if "csv" in payload and not isinstance(payload["csv"], str):
        raise DomainError("BAD_CSV", "CSV需要文本")
    events, _ = parse_payload(payload)
    context = {"parameters": payload.get("parameters", {})}
    batch_id = "MB-" + uuid.uuid4().hex[:16]
    return jsonify(scalar(store.ingest(events, batch_id, context=context)))


@bp.post("/api/materials/sample/load")
def load_sample_events():
    """把仓内样例导入空台账（已存在事件则幂等跳过）。"""
    events, _ = parse_payload(_sample_bytes())
    batch_id = "MB-SAMPLE-" + uuid.uuid4().hex[:12]
    return jsonify(scalar(store.ingest(events, batch_id)))


@bp.post("/api/materials/certificates")
def publish():
    """发布周期签证；失败整体回滚、旧版保留；同内容重发幂等。"""
    payload = request.get_json(silent=True) or {}
    period = payload.get("period")
    if not period:
        raise DomainError("BAD_PERIOD", "请提供签证周期 YYYY-MM")
    base_version = payload.get("base_version")
    context = {"parameters": payload.get("parameters", {})}
    return jsonify(scalar(store.publish_certificate(
        str(period), context=context, base_version=base_version)))


@bp.get("/api/materials/certificates")
def certificates():
    return jsonify({"certificates": store.list_certificates()})


@bp.get("/api/materials/certificates/<cert_id>")
def certificate_detail(cert_id):
    return jsonify(scalar(store.get_certificate(cert_id)))


@bp.post("/api/materials/certificates/<cert_id>/sign")
def sign(cert_id):
    payload = request.get_json(silent=True) or {}
    version = payload.get("version")
    if not isinstance(version, int):
        raise DomainError("BAD_VERSION", "请提供版本号")
    return jsonify(scalar(store.sign_certificate(
        cert_id, version, payload.get("decision", "已签认"))))


@bp.post("/api/materials/reset")
def reset():
    store.reset()
    return {"status": "ok"}
