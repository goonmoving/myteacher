#!/usr/bin/env python3
"""M1 冒烟 CLI:python -m myteacher.cli [--db myteacher.db] music

命令:题号字母(A/B/C/D)/ skip too_easy|too_hard|boring / bored /
     followup <文本> / exit
每题打印 action_type + reason(为什么是这道题);每步打印 nudge(地图为何变)。
"""
import argparse
import time

from myteacher.config import bootstrap
from myteacher.events import Engine
from myteacher.model import Event
from myteacher.judge import judge_answer
from myteacher.policy import choose_next
from myteacher.llm import StubLLM
from myteacher.store import Store


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="myteacher.db")
    ap.add_argument("domain")
    args = ap.parse_args()

    store = Store(args.db)
    bootstrap(store)
    eng = Engine(store)
    sid = eng.create_session(args.domain)
    llm = StubLLM()
    print(f"== myteacher M1 session({args.domain}) id={sid} ==")
    print("命令:答题输入 A/B/C/D;skip too_easy|too_hard|boring;bored;"
          "followup <文本>;exit")

    while True:
        try:
            choice = choose_next(store, sid, int(time.time()), llm=llm)
        except RuntimeError as e:
            print(f"[stop] {e}")
            break
        q = choice.question
        tag = "【深挖】" if choice.dive_active else ""
        print(f"\n[{choice.action_type}] {tag}{choice.reason}")
        print(f"题(q={q.id},kind={q.kind}):{q.body.get('prompt','')}")
        for i, opt in enumerate(q.body.get("options", [])):
            print(f"  {'ABCD'[i]}. {opt}")
        raw = input("> ").strip()
        if raw == "exit":
            break
        if raw.startswith("skip"):
            reason = raw.split()[1] if len(raw.split()) > 1 else None
            r = eng.apply_event(_ev(sid, "skip", q, payload={"reason": reason}))
            print(r["nudge"])
            continue
        if raw == "bored":
            r = eng.apply_event(_ev(sid, "boredom_flag", q))
            print(r["nudge"])
            if r.get("dive_exited"):
                print("(深挖模式退出)")
            continue
        if raw.startswith("followup"):
            r = eng.apply_event(_ev(sid, "followup", q, payload={"text": raw[9:].strip()}))
            print(r["nudge"])
            continue
        if raw in ("A", "B", "C", "D"):
            verdict = judge_answer(q, "ABCD".index(raw))
            if verdict.get("needs_user"):
                v = input("M1 无 LLM 判分,你自判对吗?(y/n)> ").strip().lower()
                verdict = {"correct": v == "y", "confidence": 0.0}
            ev_type = "answer_correct" if verdict["correct"] else "answer_wrong"
            r = eng.apply_event(_ev(sid, ev_type, q, payload={"self_confidence": None}))
            st = eng.store.state(q.node_id)
            print(r["nudge"])
            st_dict = st.to_dict()
            print(f"  节点状态:mastery={st_dict['mastery']} ev={st_dict['evidence_count']}"
                  f" streaks=(C{st_dict['correct_streak']}/W{st_dict['wrong_streak']}"
                  f"/S{st_dict['skip_streak']}/F{st_dict['followup_streak']})"
                  f" bias={st_dict['difficulty_bias']}")
            continue
        print("(无效输入)")


def _ev(sid, typ, q, payload=None):
    return Event(ts=int(time.time()), session_id=sid, type=typ,
                 question_id=q.id, difficulty=q.difficulty,
                 payload=payload or {})


if __name__ == "__main__":
    main()
