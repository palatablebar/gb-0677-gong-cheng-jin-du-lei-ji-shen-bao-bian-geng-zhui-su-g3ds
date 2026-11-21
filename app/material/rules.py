"""甲供钢材跨期领用退库承接规则。

裁决原则（对应业务诉求）：
1. 历史签证金额不变：以滚动承接的「未结清材料款」余额进入本期，绝不用累计领用量
   重新抵扣历史期；已关闭周期只读，旧签证金额不被重算。
2. 本期耗用是抵扣上限：本期抵扣 = min(本期耗用金额, 耗用后未结清新增池)，
   抵扣永远不超过「耗用后未结清材料款」。
3. 退库尽快冲减本期净额：退库优先冲减本材料未结清新增池；超出未结清的部分形成
   「退库待结转」，在后续期间继续冲减，不回改旧签证。
4. 同一材料不重复扣：按材料规范码逐期滚动，领用只在耗用形成可扣口径时才进入抵扣，
   未耗用余料继续挂在未结清池，绝不二次抵扣。
5. 幂等：同一 event_id 的重试只采信一次。
"""
from decimal import Decimal

from app.values import number, money, instant
from app.reasons import reject
from app.errors import DomainError

# 材料别名 -> 规范材料码，同一物理材料的不同叫法必须归一，避免重复扣。
MATERIALS = {
    "HRB400 螺纹钢": "MAT-HRB400",
    "螺纹钢": "MAT-HRB400",
    "HRB400": "MAT-HRB400",
    "Q235 圆钢": "MAT-Q235",
    "圆钢": "MAT-Q235",
    "Q235": "MAT-Q235",
}
MATERIAL_NAMES = {"MAT-HRB400": "HRB400 螺纹钢", "MAT-Q235": "Q235 圆钢"}
EVENT_TYPES = {"issue", "consume", "return", "surplus"}
# 同一周期内的固定处理次序：先领用入池、再退库冲减、然后耗用、最后结转余料。
# 次序固定保证「退库先于耗用冲减」，抵扣口径可复核、可重放。
TYPE_ORDER = {"issue": 0, "return": 1, "consume": 2, "surplus": 3}


def _period(value):
    text = str(value).strip()
    if len(text) != 7 or text[4] != "-":
        raise DomainError("BAD_PERIOD", "周期需为 YYYY-MM")
    year, month = text.split("-")
    if not (year.isdigit() and month.isdigit() and 1 <= int(month) <= 12):
        raise DomainError("BAD_PERIOD", "周期需为 YYYY-MM")
    return text


def validate(rows, closed_periods=None):
    """校验并归一化事件。合法事件进 good，问题事件隔离原值，绝不静默丢弃。"""
    closed = {str(p) for p in (closed_periods or [])}
    good, bad = [], []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            bad.append(reject({"_raw": row}, "BAD_ROW", "事件需要对象"))
            continue
        event_id = str(row.get("event_id", "")).strip()
        if not event_id:
            bad.append(reject(row, "EMPTY_VALUE", "事件编号为空"))
            continue
        if event_id in seen:
            bad.append(reject(row, "DUPLICATE_EVENT", "同一事件重复提交，重试不得重复抵扣"))
            continue
        try:
            etype = str(row.get("event_type", "")).strip()
            if etype not in EVENT_TYPES:
                raise DomainError("UNKNOWN_EVENT_TYPE", "事件类型仅支持 issue/consume/return/surplus")
            code = MATERIALS.get(str(row.get("material", "")).strip())
            if code is None:
                raise DomainError("UNKNOWN_MATERIAL", "材料未登记")
            period = _period(row.get("period"))
            if period in closed:
                raise DomainError("PERIOD_CLOSED", "周期已签认关闭，不能回改历史签证")
            qty = number(row.get("quantity"))
            if qty <= 0:
                raise DomainError("BAD_QUANTITY", "事件数量必须为正，冲销请用 return 事件")
            price = number(row.get("unit_price"))
            if price < 0:
                raise DomainError("BAD_PRICE", "单价不能为负")
            instant(row.get("occurred_at"))  # 时间必须带时区偏移
            ref_id = str(row.get("ref_id", "")).strip()
            if not ref_id:
                raise DomainError("EMPTY_VALUE", "原始单据号为空")
        except DomainError as exc:
            bad.append(reject(row, exc.code, exc.message))
            continue
        seen.add(event_id)
        good.append({
            "event_id": event_id,
            "material_code": code,
            "material": MATERIAL_NAMES[code],
            "event_type": etype,
            "period": period,
            "quantity": qty,
            "unit_price": price,
            "ref_id": ref_id,
            "occurred_at": instant(row.get("occurred_at")).isoformat(),
            "_line": row.get("_line"),
            "_sources": row.get("_sources", [row.get("_line", 0)]),
        })
    return good, bad


def _settle_period(state, period, events):
    """对单材料单周期结算，返回本期承接结果行并更新滚动状态。"""
    price = events[0]["unit_price"]
    opening_qty = state["unsettled_qty"]
    opening_credit_qty = state["credit_qty"]
    issue_qty = consume_qty = return_qty = surplus_qty = Decimal(0)
    source_lines, event_ids = [], []
    ordered = sorted(events, key=lambda e: (TYPE_ORDER[e["event_type"]], e["event_id"]))

    # 第一阶段：只校验与汇总，不改滚动余额。同周期单价冲突直接隔离整组。
    for ev in ordered:
        if ev["unit_price"] != price:
            raise DomainError("PRICE_CONFLICT", "同材料同周期单价冲突，需人工裁定")
        qty = ev["quantity"]
        etype = ev["event_type"]
        if etype == "issue":
            issue_qty += qty
        elif etype == "return":
            return_qty += qty
        elif etype == "consume":
            consume_qty += qty
        else:  # surplus：盘点余料，仍属未结清新增池，不改变余额，只登记可追溯。
            surplus_qty += qty
        source_lines.extend(ev.get("_sources", []))
        event_ids.append(ev["event_id"])

    # 固定次序：先领用入池、再退库冲减、然后耗用、余料仅登记。
    # 退库先冲未结池，超额部分挂「退库待结转」，后续期继续冲，不回改旧签证。
    state["unsettled_qty"] += issue_qty
    return_absorbed_qty = min(return_qty, state["unsettled_qty"])
    state["unsettled_qty"] -= return_absorbed_qty
    return_carry_qty = return_qty - return_absorbed_qty

    # 耗用必须有未结池作为领用依据，防止无领用凭空抵扣。
    if consume_qty > state["unsettled_qty"]:
        raise DomainError("CONSUME_EXCEEDS_LEDGER", "耗用超过可用未结清新增池，缺少领用依据")
    state["unsettled_qty"] -= consume_qty

    # 本期可签认抵扣量：两道口径同时约束。
    # 口径一（审计）：抵扣不超过本期耗用，未耗用余料不得提前抵扣。
    # 口径二（财务）：抵扣不超过含本期领用、已被退库冲减后的未结池。
    available_pool = opening_qty + issue_qty - return_absorbed_qty
    deduct_qty = min(consume_qty, available_pool)
    if deduct_qty < 0:
        deduct_qty = Decimal(0)

    # 前期退库待结转在本期继续冲减未结池（跨期承接，不动旧签证）。
    credit_used_qty = min(opening_credit_qty, state["unsettled_qty"])
    state["unsettled_qty"] -= credit_used_qty
    state["credit_qty"] = opening_credit_qty - credit_used_qty + return_carry_qty

    consume_amount = consume_qty * price
    deduct_amount = deduct_qty * price
    return_credit_amount = (return_absorbed_qty + credit_used_qty) * price
    return_carry_amount = state["credit_qty"] * price
    remaining_deductible_amount = state["unsettled_qty"] * price

    return {
        "period": period,
        "material_code": events[0]["material_code"],
        "material": events[0]["material"],
        "unit_price": money(price),
        "opening_unsettled_qty": str(opening_qty),
        "issue_qty": str(issue_qty),
        "consume_qty": str(consume_qty),
        "return_qty": str(return_qty),
        "surplus_qty": str(surplus_qty),
        # 本期耗用（金额）：财务/审计核对的耗用口径。
        "period_consume_amount": money(consume_amount),
        # 本期可签认抵扣：不超过耗用后未结清新增材料款。
        "period_deduct_amount": money(deduct_amount),
        # 退库承接：本期实际冲减净额（本期退库冲池 + 前期退库待结转在本期承接）。
        "return_credit_amount": money(return_credit_amount),
        "return_carry_forward_amount": money(return_carry_amount),
        # 剩余可扣：期末未结清新增材料款，结转下期继续抵扣。
        "remaining_deductible_amount": money(remaining_deductible_amount),
        "closing_unsettled_qty": str(state["unsettled_qty"]),
        "event_ids": event_ids,
        "_sources": sorted(set(source_lines)),
    }


def evaluate(rows, context=None):
    """纯函数：把归一化事件按材料逐期滚动结算，输出可签认的材料抵扣结果。"""
    context = context or {}
    # 允许通过参数注入期初未结清新增池（新系统承接老台账），不允许注入负数。
    opening = context.get("parameters", {}).get("opening_unsettled", {})
    states = {}
    for code, qty_text in opening.items():
        qty = number(qty_text)
        if qty < 0:
            raise DomainError("BAD_OPENING", "期初未结清新增量不能为负")
        states[code] = {"unsettled_qty": qty, "credit_qty": Decimal(0)}

    grouped = {}
    for ev in rows:
        grouped.setdefault((ev["material_code"], ev["period"]), []).append(ev)

    output, quarantine = [], []
    # 必须先按周期、再按材料排序：同一材料的滚动余额依赖时间顺序承接。
    for code, period in sorted(grouped.keys(), key=lambda k: (k[1], k[0])):
        state = states.setdefault(code, {"unsettled_qty": Decimal(0), "credit_qty": Decimal(0)})
        snapshot = {"unsettled_qty": state["unsettled_qty"], "credit_qty": state["credit_qty"]}
        try:
            output.append(_settle_period(state, period, grouped[(code, period)]))
        except DomainError as exc:
            # 整组结算失败：回滚该组对滚动状态的改动，隔离原事件，不影响其他材料/周期。
            state["unsettled_qty"] = snapshot["unsettled_qty"]
            state["credit_qty"] = snapshot["credit_qty"]
            for ev in grouped[(code, period)]:
                quarantine.append(reject(ev, exc.code, exc.message))

    total_consume = sum((number(r["period_consume_amount"]) for r in output), Decimal(0))
    total_deduct = sum((number(r["period_deduct_amount"]) for r in output), Decimal(0))
    total_credit = sum((number(r["return_credit_amount"]) for r in output), Decimal(0))
    total_remaining = sum((number(r["remaining_deductible_amount"]) for r in output), Decimal(0))
    total_carry = sum((number(r["return_carry_forward_amount"]) for r in output), Decimal(0))
    metrics = {
        "material_kinds": len(set(r["material_code"] for r in output)),
        "period_records": len(output),
        "period_consume_total": money(total_consume),
        "period_deduct_total": money(total_deduct),
        "return_credit_total": money(total_credit),
        "return_carry_forward_total": money(total_carry),
        "remaining_deductible_total": money(total_remaining),
        # 抵扣不得超过耗用；守恒校验供工作台展示可签认状态。
        "deduction_within_consume": total_deduct <= total_consume,
        "balanced": not quarantine,
    }
    return {"rows": output, "quarantine": quarantine, "metrics": metrics}


def adjudicate(events, context=None, closed_periods=None):
    """入口：校验 + 承接裁决一步完成，隔离行随结果返回。"""
    good, bad = validate(events, closed_periods=closed_periods)
    result = evaluate(good, context)
    result["quarantine"] = bad + result["quarantine"]
    result["metrics"]["quarantine"] = len(result["quarantine"])
    result["metrics"]["accepted_events"] = len(good)
    result["metrics"]["balanced"] = not result["quarantine"]
    return result
