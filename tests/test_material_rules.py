"""甲供材跨期领用退库承接的裁决规则测试。"""
from decimal import Decimal

import pytest

from app.errors import DomainError
from app.material.intake import parse_payload
from app.material.rules import adjudicate, validate, evaluate
from app.config import ROOT


def _ev(event_id, etype, material="螺纹钢", period="2026-11",
        qty="10", price="4200", ref="RD-1", ts="2026-11-08T09:00:00+08:00"):
    return {"event_id": event_id, "event_type": etype, "material": material,
            "period": period, "quantity": qty, "unit_price": price,
            "ref_id": ref, "occurred_at": ts, "_line": 1, "_sources": [1]}


@pytest.fixture
def sample_events():
    raw = (ROOT / "sample_data/material_events.csv").read_bytes()
    events, _ = parse_payload(raw)
    return events


# ---------- 承接裁决：本期耗用 / 退库承接 / 剩余可扣 ----------

def test_sample_balanced_and_within_consume(sample_events):
    r = adjudicate(sample_events)
    assert r["metrics"]["balanced"] is True
    assert r["metrics"]["deduction_within_consume"] is True
    assert r["metrics"]["quarantine"] == 0


def test_period_consume_return_remaining_split(sample_events):
    rows = {(x["period"], x["material_code"]): x for x in adjudicate(sample_events)["rows"]}
    oct_hrb = rows[("2026-10", "MAT-HRB400")]
    # 领用100、耗用80、退库10：本期耗用336000，退库冲减42000，剩余可扣=10t*4200。
    assert oct_hrb["period_consume_amount"] == "336000.00"
    assert oct_hrb["period_deduct_amount"] == "336000.00"
    assert oct_hrb["return_credit_amount"] == "42000.00"
    assert oct_hrb["remaining_deductible_amount"] == "42000.00"


def test_balance_rolls_forward_across_periods(sample_events):
    rows = {(x["period"], x["material_code"]): x for x in adjudicate(sample_events)["rows"]}
    # 10月期末未结10t，必须作为11月期初承接，绝不按累计领用量重算历史签证。
    nov_hrb = rows[("2026-11", "MAT-HRB400")]
    assert nov_hrb["opening_unsettled_qty"] == "10"
    assert nov_hrb["remaining_deductible_amount"] == "63000.00"  # 15t * 4200


def test_over_return_carries_forward_then_absorbed(sample_events):
    """退库超过未结池：超额挂退库待结转，下期继续冲，旧签证金额不变。"""
    rows = {(x["period"], x["material_code"]): x for x in adjudicate(sample_events)["rows"]}
    dec_q = rows[("2026-12", "MAT-Q235")]
    # 未结池30t全冲(117000)，超额10t结转(39000)，本期不产生负抵扣。
    assert dec_q["return_credit_amount"] == "117000.00"
    assert dec_q["return_carry_forward_amount"] == "39000.00"
    assert dec_q["period_deduct_amount"] == "0.00"
    jan_q = rows[("2027-01", "MAT-Q235")]
    # 下期领用入池后，待结转的39000在本期承接冲减，结转清零。
    assert jan_q["return_credit_amount"] == "39000.00"
    assert jan_q["return_carry_forward_amount"] == "0.00"


def test_deduction_never_exceeds_consumption():
    """大量领用但仅小量耗用：未耗用余料留在剩余可扣，不得提前抵扣。"""
    events = [_ev("E1", "issue", qty="100"), _ev("E2", "consume", qty="20")]
    row = adjudicate(events)["rows"][0]
    assert row["period_deduct_amount"] == "84000.00"   # 仅20t可扣
    assert row["remaining_deductible_amount"] == "336000.00"  # 80t余料结转


def test_surplus_does_not_change_unsettled_pool():
    events = [_ev("E1", "issue", qty="50"), _ev("E2", "consume", qty="30"),
              _ev("E3", "surplus", qty="5")]
    row = adjudicate(events)["rows"][0]
    # 余料5t仍在未结池：期末未结=50-30=20t，余料登记但不抵扣。
    assert row["closing_unsettled_qty"] == "20"
    assert row["surplus_qty"] == "5"


def test_material_alias_normalized_no_double_deduction():
    """同一材料不同叫法归一到一个材料码，避免同一材料重复扣。"""
    events = [_ev("E1", "issue", material="螺纹钢", qty="40"),
              _ev("E2", "issue", material="HRB400 螺纹钢", qty="10"),
              _ev("E3", "consume", material="HRB400", qty="30")]
    r = adjudicate(events)
    assert r["metrics"]["material_kinds"] == 1
    assert r["rows"][0]["issue_qty"] == "50"


def test_consume_without_ledger_basis_quarantined():
    events = [_ev("E1", "consume", qty="10")]  # 无领用依据
    r = adjudicate(events)
    assert r["rows"] == []
    assert r["quarantine"][0]["reason"] == "CONSUME_EXCEEDS_LEDGER"


def test_repeated_event_id_is_idempotent_quarantine():
    events = [_ev("E1", "issue", qty="10"), _ev("E1", "issue", qty="10")]
    good, bad = validate(events)
    assert len(good) == 1 and bad[0]["reason"] == "DUPLICATE_EVENT"


def test_closed_period_event_rejected():
    events = [_ev("E1", "issue", period="2026-10", qty="10")]
    good, bad = validate(events, closed_periods=["2026-10"])
    assert good == [] and bad[0]["reason"] == "PERIOD_CLOSED"


def test_invalid_rows_quarantined_not_dropped():
    events = [
        _ev("E1", "issue", qty="10"),
        {"event_id": "E2", "event_type": "wat", "material": "螺纹钢",
         "period": "2026-11", "quantity": "1", "unit_price": "4200",
         "ref_id": "x", "occurred_at": "2026-11-08T09:00:00+08:00"},
        {"event_id": "E3", "event_type": "issue", "material": "未知钢",
         "period": "2026-11", "quantity": "1", "unit_price": "4200",
         "ref_id": "x", "occurred_at": "2026-11-08T09:00:00+08:00"},
    ]
    r = adjudicate(events)
    reasons = {q["reason"] for q in r["quarantine"]}
    assert "UNKNOWN_EVENT_TYPE" in reasons and "UNKNOWN_MATERIAL" in reasons
    assert len(r["rows"]) == 1


def test_negative_or_zero_quantity_rejected():
    r = adjudicate([_ev("E1", "issue", qty="-5")])
    assert r["quarantine"][0]["reason"] == "BAD_QUANTITY"


def test_timezone_required():
    bad = dict(_ev("E1", "issue")); bad["occurred_at"] = "2026-11-08 09:00:00"
    r = adjudicate([bad])
    assert r["quarantine"][0]["reason"] == "INVALID_TIME"


def test_price_conflict_quarantined_and_state_rolled_back():
    events = [_ev("E1", "issue", price="4200", qty="10"),
              _ev("E2", "consume", price="4300", qty="5")]
    r = adjudicate(events)
    assert r["quarantine"][0]["reason"] == "PRICE_CONFLICT"
    assert r["rows"] == []


def test_empty_evaluation_zero_totals():
    m = evaluate([])["metrics"]
    assert m["period_deduct_total"] == "0.00"
    assert m["remaining_deductible_total"] == "0.00"
    assert m["balanced"] is True


def test_decimal_money_quantized_to_cent():
    events = [_ev("E1", "issue", price="4200.005", qty="3"),
              _ev("E2", "consume", price="4200.005", qty="1")]
    row = adjudicate(events)["rows"][0]
    # 金额按分量化，不出现浮点尾数。
    assert Decimal(row["period_deduct_amount"]) == Decimal("4200.01")


def test_csv_parse_and_empty_states():
    with pytest.raises(DomainError) as e:
        parse_payload(b"")
    assert e.value.code == "EMPTY_FILE"
    with pytest.raises(DomainError) as e:
        parse_payload("event_id,material,event_type,period,quantity,unit_price,ref_id,occurred_at\n")
    assert e.value.code == "HEADER_ONLY"


def test_quantity_conservation_per_material(sample_events):
    """物料守恒：累计领用 = 累计耗用 + 累计退库 + 期末未结 + 期末退库待结转（折量）。"""
    r = adjudicate(sample_events)
    by_material = {}
    for row in r["rows"]:
        m = by_material.setdefault(row["material_code"], {
            "issue": Decimal(0), "consume": Decimal(0), "return": Decimal(0)})
        m["issue"] += Decimal(row["issue_qty"])
        m["consume"] += Decimal(row["consume_qty"])
        m["return"] += Decimal(row["return_qty"])
    last = {}
    for row in r["rows"]:
        last[row["material_code"]] = row
    for code, m in by_material.items():
        closing = Decimal(last[code]["closing_unsettled_qty"])
        carry = Decimal(last[code]["return_carry_forward_amount"]) / Decimal(
            last[code]["unit_price"])
        assert m["issue"] == m["consume"] + m["return"] + closing + carry


def test_historical_certificate_amount_never_recomputed(sample_events):
    """加入后续周期事件后，早期周期的裁决结果行保持不变。"""
    first = adjudicate(sample_events)
    oct_before = [r for r in first["rows"] if r["period"] == "2026-10"]
    later_events = sample_events + [_ev(
        "NEW1", "issue", material="螺纹钢", period="2027-02", qty="10")]
    second = adjudicate(later_events)
    oct_after = [r for r in second["rows"] if r["period"] == "2026-10"]
    assert oct_before == oct_after
