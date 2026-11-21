"""甲供材事件的 CSV / JSON 采信入口。"""
import csv
import io

from app.errors import DomainError
from app.modules.m1_claims.rules import decode

EVENT_COLUMNS = [
    "event_id", "material", "event_type", "period",
    "quantity", "unit_price", "ref_id", "occurred_at",
]


def parse_csv(raw):
    """解析事件 CSV，返回带物理行号的原始事件列表。"""
    if isinstance(raw, str):
        raw = raw.encode()
    text, encoding = decode(raw)
    if not text:
        raise DomainError("EMPTY_FILE", "文件为空")
    if not text.strip():
        raise DomainError("BLANK_ONLY", "全部为空行")
    stream = csv.reader(io.StringIO(text, newline=""))
    header = next(stream, None)
    if header != EVENT_COLUMNS:
        raise DomainError("BAD_HEADER", "事件表头不匹配")
    rows, blank, repeated, count = [], 0, 0, 0
    for cells in stream:
        count += 1
        line = stream.line_num
        if not cells or not any(c.strip() for c in cells):
            blank += 1
            continue
        if cells == header:
            repeated += 1
            continue
        if len(cells) != len(EVENT_COLUMNS):
            rows.append({"_line": line, "_sources": [line], "_bad_columns": True,
                         "_raw": cells})
            continue
        row = {"_line": line, "_sources": [line]}
        row.update(dict(zip(EVENT_COLUMNS, [x.strip() for x in cells])))
        rows.append(row)
    if count == 0:
        raise DomainError("HEADER_ONLY", "仅有表头")
    if not rows:
        raise DomainError("BLANK_ONLY", "全部为空行")
    return rows, {"encoding": encoding, "input": count, "blank": blank,
                  "repeated_headers": repeated}


def parse_payload(payload):
    """统一入口：支持 {events:[...]} 对象或原始 CSV 字节/文本。"""
    if isinstance(payload, (bytes, str)):
        return parse_csv(payload)
    if not isinstance(payload, dict):
        raise DomainError("BAD_BODY", "请求需要对象")
    if "csv" in payload:
        return parse_csv(payload["csv"])
    events = payload.get("events", [])
    if not isinstance(events, list) or any(not isinstance(r, dict) for r in events):
        raise DomainError("BAD_EVENTS", "events 需要对象列表")
    for index, row in enumerate(events, 1):
        row.setdefault("_line", index)
        row.setdefault("_sources", [index])
    return events, {"encoding": "json", "input": len(events), "blank": 0,
                    "repeated_headers": 0}
