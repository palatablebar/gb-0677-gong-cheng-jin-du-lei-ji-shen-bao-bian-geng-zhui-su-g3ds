# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。/api/runs使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。/api/upload接收multipart文件file。/api/runs/<id>/archive只导出一份带清单与证据的CSV。

## 甲供材跨期承接（独立于七阶段流水线）

| 方法与路径 | 作用 |
|---|---|
| GET /materials | 甲供材工作台页面 |
| GET /api/materials/ledger | 库内权威台账滚动裁决；空库回退样例预览（不落库） |
| POST /api/materials/events | 事件入账，body 为 `{events:[...]}` 或 `{csv:"..."}`；event_id 幂等，重试跳过不重复抵扣 |
| POST /api/materials/sample/load | 载入仓内 `sample_data/material_events.csv` |
| POST /api/materials/certificates | 发布周期签证，body `{period, base_version?}`；同内容重发幂等，陈旧基版本409且旧版保留 |
| GET /api/materials/certificates | 签证清单（版本、状态、摘要） |
| GET /api/materials/certificates/<cert_id> | 签证快照明细 |
| POST /api/materials/certificates/<cert_id>/sign | 签认/退回，body `{version, decision}`，乐观锁，陈旧版本409 |
| POST /api/materials/reset | 清空台账（仅测试/重置） |

事件列：event_id, material, event_type(issue/consume/return/surplus), period(YYYY-MM), quantity, unit_price, ref_id, occurred_at（必须带时区偏移）。被拒事件（关闭期、无领用依据耗用、单价冲突等）只隔离不落库；已签认周期只读，补录事件返回 PERIOD_CLOSED 且不重算历史签证。
