#!/usr/bin/env python3
"""M4 调参 harness(SPEC §12 / §5.1):合成用户闭环,网格搜索权重。

用法:
  python3 -m myteacher.tune --domain music --rounds 240
真实数据模式(有 events.log 后):
  python3 -m myteacher.tune --log events.log --domain music
指标:
  goal_err  = 各次出题 |difficulty − 该节点真实能力| 均值(↓好;skip 也计)
  map_err   = 收官 |mastery − 真实能力| 均值(↓好)
  coverage  = 覆盖的叶节点比例(↑好)
  bore_ok   = 「连跳≥3 后下一题偏航」遵守率(↑好,SPEC §11.3)
本 harness 只读不改地图:模拟事件走 Engine(唯一写入口),与真实使用同构。
"""
import argparse
import json
import math
import random
from dataclasses import asdict

from myteacher.config import REPO_ROOT, bootstrap
from myteacher.events import Engine, SECONDS_PER_DAY
from myteacher.model import Event
from myteacher.policy import Weights, choose_next
from myteacher.store import Store
from myteacher.replay import load_log

T0 = 1_760_000_000
ROUND_SECONDS = 3600


def make_truth(domain, seed):
    """合成用户:每叶节点固定真实能力(0-100);同 seed 稳定。"""
    s = Store(":memory:")
    bootstrap(s, REPO_ROOT)
    leaves = [n.id for n in s.nodes(domain) if n.parent]
    rng = random.Random(seed)
    return {nid: round(30 + rng.random() * 60, 1) for nid in leaves}, leaves


def simulate(domain="music", weights=None, rounds=240, seed=0):
    truth, leaves = make_truth(domain, seed)
    store = Store(":memory:")
    bootstrap(store, REPO_ROOT)
    eng = Engine(store, now=lambda: T0)
    sid = eng.create_session(domain)
    rng = random.Random(seed + 1)
    goal_errs = []
    served_nodes = set()
    bore_ok = bore_total = 0

    for i in range(rounds):
        t = T0 + i * ROUND_SECONDS
        last5 = store.events(session_id=sid, ascending=False, limit=5)
        skip5 = sum(1 for e in last5 if e.type == "skip")
        prev_node, prev_diff = (store.session(sid).last_q_node,
                                store.session(sid).last_q_difficulty)
        if skip5 >= 3 and prev_node is not None:
            bore_total += 1
        try:
            choice = choose_next(store, sid, t, weights=weights)
        except RuntimeError:
            break
        q = choice.question
        nid, diff = q.node_id, q.difficulty
        if prev_node == nid and abs(diff - (prev_diff or 0)) < 15:
            pass
        elif bore_total and skip5 >= 3:
            bore_ok += 1
        served_nodes.add(nid)
        truth_v = truth[nid]
        goal_errs.append(abs(diff - truth_v))

        p = 1.0 / (1.0 + math.exp(-(truth_v - diff) / 12.0))
        roll = rng.random()
        if truth_v - diff > 30 and roll < 0.6:
            eng.apply_event(Event(ts=t, session_id=sid, type="skip",
                                  question_id=q.id, node_id=nid,
                                  difficulty=diff, payload={"reason": "boring"}))
            continue
        e_type = "answer_correct" if roll < p else "answer_wrong"
        eng.apply_event(Event(ts=t, session_id=sid, type=e_type,
                              question_id=q.id, node_id=nid,
                              difficulty=diff,
                              payload={"self_confidence": rng.choice((2, 3, 3, 4))}))

    states = store.states()
    map_err = (sum(abs(st.mastery - truth[nid]) for nid, st in states.items()
                   if st.evidence_count > 0) /
               max(1, sum(1 for st in states.values() if st.evidence_count > 0)))
    coverage = len(served_nodes) / max(1, len(leaves))
    return {"weights": asdict(weights) if weights else {},
            "goal_err": round(sum(goal_errs) / max(1, len(goal_errs)), 2),
            "map_err": round(map_err, 2),
            "coverage": round(coverage, 3),
            "bore_ok": round(bore_ok / max(1, bore_total), 3),
            "rounds_done": len(goal_errs)}


def score(m):
    """综合:map_err 为主,goal_err 辅;越低越好。"""
    return round(m["map_err"] + 0.3 * m["goal_err"], 3)


CANDIDATES = {
    "base":         Weights(20, 20, 25, 15, 30),
    "rec-heavy":    Weights(60, 20, 25, 15, 30),
    "below-heavy":  Weights(20, 20, 45, 15, 30),
    "bore-light":   Weights(20, 20, 25, 15, 10),
    "balanced":     Weights(25, 20, 20, 15, 35),
}


def grid(domain="music", rounds=240, seed=0):
    rows = []
    for name, w in CANDIDATES.items():
        m = simulate(domain, w, rounds, seed)
        rows.append({"config": name, "score": score(m), **m})
    rows.sort(key=lambda r: r["score"])
    return rows


def real_log_mode(log_path, domain="music"):
    """真实 events.log 模式:重放 → 只报 coverage/事件构成(无真值)。"""
    store = Store(":memory:")
    bootstrap(store, REPO_ROOT)
    with open(log_path, encoding="utf-8") as f:
        log = [json.loads(x) for x in f if x.strip()]
    load_log(store, log)
    dom_ev = [e for e in log if (e.get("node_id") or "").startswith(domain + ".")]
    from myteacher.diagnostics import report
    nodes = store.nodes(domain)
    served = {e.get("question_id") for e in dom_ev if e.get("question_id")}
    q_nodes = {store.question(q).node_id for q in served if store.question(q)}
    return {"events": len(dom_ev),
            "distinct_nodes_hit": len(q_nodes),
            "leaf_nodes": len([n for n in nodes if n.parent]),
            "diagnostics": report(store, domain)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="music")
    ap.add_argument("--rounds", type=int, default=240)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--log", default=None, help="真实 events.log(JSONL)")
    args = ap.parse_args()
    if args.log:
        print(json.dumps(real_log_mode(args.log, args.domain),
                         ensure_ascii=False, indent=1))
        return
    rows = grid(args.domain, args.rounds, args.seed)
    print(f"== 参数网格({args.domain},{args.rounds} 轮)==")
    print(f"{'config':<12} {'score':>7} {'map_err':>8} {'goal_err':>8} "
          f"{'coverage':>8} {'bore_ok':>8}")
    for r in rows:
        print(f"{r['config']:<12} {r['score']:>7.2f} {r['map_err']:>8.2f} "
              f"{r['goal_err']:>8.2f} {r['coverage']:>8.2f} {r['bore_ok']:>8.2f}")
    best = rows[0]
    print(f"→ 推荐:{best['config']}(score {best['score']});"
          f"把 Weights 设为 above CANDIDATES['{best['config']}'] 作为 §5.1 默认。")


if __name__ == "__main__":
    main()
