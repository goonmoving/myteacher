"""§14 数据派生指标:迁移诊断 / 保持曲线 / 自信校准。

铁律(SPEC §14.4):纯离线分析,只读 events+questions,不回流地图状态。
事件要求(SPEC §11.8):answer_* 事件带 question_id(join kind)、
payload.mastery_after / payload.self_confidence(可选)。
结论模板按 §14 走;阈值常量在此,可调。
"""
from myteacher.events import SECONDS_PER_DAY
from myteacher.model import QUESTION_KINDS

MIN_KIND_ATTEMPTS = 3        # 每类口径最少尝试数
GAP_PCT = 20                 # 迁移差距触发阈值(百分点)
RETENTION_BINS = (("<1d", 0, 1), ("1-7d", 1, 7), ("7-30d", 7, 30), ("30d+", 30, 10**9))
RETENTION_EARLY_ACC = 0.8
RETENTION_LATE_ACC = 0.6
RETENTION_MIN_EACH = 2       # 早/晚桶各自最少样本
CALIB_GAP_PCT = 15           # 自信校准触发阈值(百分点)
CONF_LEVELS = (1, 2, 3, 4)   # 1=瞎猜 … 4=确定


def _acc(corrects, attempts):
    return round(corrects / attempts, 4) if attempts else None


def transfer_diagnosis(store, domain: str) -> dict:
    """14.1:join question.kind → per node × kind & 全域 per kind 正确率。"""
    qcache = {}
    per_node = {}
    per_kind = {k: [0, 0] for k in QUESTION_KINDS}   # kind → [correct, attempts]
    for ev in store.events():
        if ev.type not in ("answer_correct", "answer_wrong"):
            continue
        node = ev.node_id or ""
        if not node.startswith(domain + "."):
            continue
        q = qcache.get(ev.question_id) or store.question(ev.question_id or "")
        qcache[ev.question_id] = q
        kind = q.kind if q else "unknown"
        ok = 1 if ev.type == "answer_correct" else 0
        per_kind.setdefault(kind, [0, 0])
        per_kind[kind][0] += ok
        per_kind[kind][1] += 1
        row = per_node.setdefault(node, {k: [0, 0] for k in
                                         list(QUESTION_KINDS) + ["unknown"]})
        row.setdefault(kind, [0, 0])
        row[kind][0] += ok
        row[kind][1] += 1
    insights = []
    gaps = []
    for node, kinds in sorted(per_node.items()):
        c, v = kinds.get("concept", [0, 0]), kinds.get("transfer", [0, 0])
        if c[1] >= MIN_KIND_ATTEMPTS and v[1] >= MIN_KIND_ATTEMPTS:
            gap = (c[0] / c[1] - v[0] / v[1]) * 100
            if gap >= GAP_PCT:
                gaps.append({"node": node,
                             "concept_acc": round(c[0] / c[1], 3),
                             "transfer_acc": round(v[0] / v[1], 3),
                             "gap_pct": round(gap, 1)})
    if gaps:
        insights.append({"code": "transfer_gap",
                         "message": "缺的不是理解,是迁移",
                         "nodes": gaps})
    return {
        "by_kind": {k: {"attempted": v[1],
                        "acc": _acc(v[0], v[1])} for k, v in per_kind.items()},
        "by_node_gaps": gaps,
        "insights": insights,
    }


def retention_diagnosis(store, domain: str) -> dict:
    """14.2:同节点相邻两次 answer_* 的间隔 → 正确率分桶 1/7/30 天。"""
    last_ts = {}
    buckets = {name: [0, 0] for name, _, _ in RETENTION_BINS}
    for ev in store.events():
        if ev.type not in ("answer_correct", "answer_wrong"):
            continue
        node = ev.node_id or ""
        if not node.startswith(domain + "."):
            continue
        ok = 1 if ev.type == "answer_correct" else 0
        prev = last_ts.get(node)
        if prev is not None:
            days = (ev.ts - prev) / SECONDS_PER_DAY
            for name, lo, hi in RETENTION_BINS:
                if lo <= days < hi:
                    buckets[name][0] += ok
                    buckets[name][1] += 1
                    break
        last_ts[node] = ev.ts
    rows = {name: {"attempted": v[1], "acc": _acc(v[0], v[1])}
            for name, v in buckets.items()}
    early = buckets["<1d"]
    late = buckets["30d+"]
    insights = []
    if (early[1] >= RETENTION_MIN_EACH and late[1] >= RETENTION_MIN_EACH
            and early[0] / early[1] >= RETENTION_EARLY_ACC
            and late[0] / late[1] <= RETENTION_LATE_ACC):
        insights.append({"code": "retention_weak",
                         "message": "理解没问题,长期保持不足",
                         "detail": {"early_acc": round(early[0] / early[1], 3),
                                    "late_acc": round(late[0] / late[1], 3)}})
    return {"buckets": rows, "insights": insights}


def confidence_diagnosis(store, domain: str) -> dict:
    """14.3:self_confidence(1-4) vs 判分 → 校准与高估检测。"""
    by_level = {lv: [0, 0] for lv in CONF_LEVELS}   # level → [correct, attempts]
    for ev in store.events():
        if ev.type not in ("answer_correct", "answer_wrong"):
            continue
        node = ev.node_id or ""
        if not node.startswith(domain + "."):
            continue
        conf = ev.payload.get("self_confidence")
        if not isinstance(conf, int) or conf not in CONF_LEVELS:
            continue
        ok = 1 if ev.type == "answer_correct" else 0
        by_level[conf][0] += ok
        by_level[conf][1] += 1
    rows = {}
    impl_sum = impl_n = act_sum = act_n = 0
    for lv in CONF_LEVELS:
        c, a = by_level[lv]
        rows[lv] = {"attempted": a, "acc": _acc(c, a)}
        if a:
            impl_sum += (lv / 4) * 100 * a
            impl_n += a
            act_sum += (c / a) * 100 * a
            act_n += a
    insights = []
    if impl_n >= MIN_KIND_ATTEMPTS:
        implied = round(impl_sum / impl_n, 1)
        actual = round(act_sum / act_n, 1)
        gap = round(implied - actual, 1)
        if gap >= CALIB_GAP_PCT:
            insights.append({"code": "overconfidence",
                             "message": "高估了掌握程度",
                             "detail": {"implied_confidence_pct": implied,
                                        "actual_acc_pct": actual, "gap_pct": gap}})
        elif gap <= -CALIB_GAP_PCT:
            insights.append({"code": "underconfidence",
                             "message": "低估了掌握程度(不妨大胆一点)",
                             "detail": {"implied_confidence_pct": implied,
                                        "actual_acc_pct": actual, "gap_pct": gap}})
    return {"by_level": {str(k): v for k, v in rows.items()},
            "insights": insights}


def report(store, domain: str) -> dict:
    """三类指标合集(§14)+ 基础计量。不写任何状态。"""
    evs = [e for e in store.events()]
    dom_ev = [e for e in evs if (e.node_id or "").startswith(domain + ".")]
    return {
        "domain": domain,
        "based_on_events": len(dom_ev),
        "transfer": transfer_diagnosis(store, domain),
        "retention": retention_diagnosis(store, domain),
        "confidence": confidence_diagnosis(store, domain),
    }
