"""§14 诊断三件套(迁移/保持曲线/自信校准)精确验收 + 纯函数性。"""
import unittest

from myteacher.config import REPO_ROOT, bootstrap
from myteacher.diagnostics import (report, retention_diagnosis,
                                   transfer_diagnosis, confidence_diagnosis)
from myteacher.events import Engine
from myteacher.model import Event, Question
from myteacher.replay import snapshot
from myteacher.store import Store

T0 = 1_760_000_000
H = 3600
D = 86_400

Q_CONCEPT = Question(id="q_t_concept", node_id="music.beat", difficulty=40,
                     format="mcq", kind="concept",
                     body={"prompt": "c"}, answer_key={"answer": 0})
Q_TRANSFER = Question(id="q_t_transfer", node_id="music.beat",
                      difficulty=70, kind="transfer", format="mcq",
                      body={"prompt": "t"}, answer_key={"answer": 0},
                      qc_status="approved")


def fresh():
    s = Store(":memory:")
    bootstrap(s, REPO_ROOT)
    s.add_question(Q_CONCEPT)
    s.add_question(Q_TRANSFER)
    e = Engine(s, now=lambda: T0)
    sid = e.create_session("music")
    return s, e, sid


def feed(s, e, sid):
    """构造 §11.8 语义的事件流:
    - beat 概念题:t0..t0+25h 内 3 对 1 错(改答案,折半) → <1d 桶
    - beat 迁移题:t0+50d 三连错                      → 30d+ 桶
    - rhythm 带 self_confidence=4 的 4 题(1 对 3 错) → 高估
    """
    def ev(ts, typ, q, payload=None):
        e.apply_event(Event(ts=ts, session_id=sid, type=typ, question_id=q.id,
                            node_id=q.node_id, difficulty=q.difficulty,
                            payload=payload or {}))
    ev(T0, "answer_correct", Q_CONCEPT)
    ev(T0 + 12 * H, "answer_correct", Q_CONCEPT)
    ev(T0 + 20 * H, "answer_correct", Q_CONCEPT)
    ev(T0 + 3 * D, "answer_wrong", Q_CONCEPT, {"changed_from": 0})
    ev(T0 + 50 * D, "answer_wrong", Q_TRANSFER)
    ev(T0 + 80 * D, "answer_wrong", Q_TRANSFER)
    ev(T0 + 110 * D, "answer_wrong", Q_TRANSFER)
    # 自信校准协议题放 literature 域(诊断按域过滤,curricular互不污染)
    for i, ok in enumerate([0, 0, 0, 1]):
        q = s.candidate_questions("literature.lang_rhythm")[0]
        e.apply_event(Event(ts=T0 + i * H, session_id=sid,
                            type="answer_correct" if ok else "answer_wrong",
                            question_id=q.id, node_id=q.node_id,
                            difficulty=q.difficulty,
                            payload={"self_confidence": 4}))


class Transfer(unittest.TestCase):
    def test_gap_detected(self):
        s, e, sid = fresh()
        feed(s, e, sid)
        r = transfer_diagnosis(s, "music")
        self.assertEqual(r["by_kind"]["concept"]["attempted"], 4)
        self.assertAlmostEqual(r["by_kind"]["concept"]["acc"], 0.75)
        self.assertEqual(r["by_kind"]["transfer"]["attempted"], 3)
        self.assertAlmostEqual(r["by_kind"]["transfer"]["acc"], 0.0)
        codes = [i["code"] for i in r["insights"]]
        self.assertIn("transfer_gap", codes)
        gap = r["by_node_gaps"][0]
        self.assertEqual(gap["node"], "music.beat")
        self.assertAlmostEqual(gap["gap_pct"], 75.0, places=3)


class Retention(unittest.TestCase):
    def test_weak_longterm_detected(self):
        s, e, sid = fresh()
        feed(s, e, sid)
        r = retention_diagnosis(s, "music")
        self.assertEqual(r["buckets"]["<1d"]["attempted"], 2)   # e2(12h)+e3(20h)
        self.assertAlmostEqual(r["buckets"]["<1d"]["acc"], 1.0)
        self.assertEqual(r["buckets"]["30d+"]["attempted"], 3)
        self.assertAlmostEqual(r["buckets"]["30d+"]["acc"], 0.0)
        codes = [i["code"] for i in r["insights"]]
        self.assertIn("retention_weak", codes)


class Confidence(unittest.TestCase):
    def test_overconfidence_detected(self):
        s, e, sid = fresh()
        feed(s, e, sid)
        r = confidence_diagnosis(s, "literature")
        self.assertEqual(r["by_level"]["4"]["attempted"], 4)
        self.assertAlmostEqual(r["by_level"]["4"]["acc"], 0.25)
        codes = [i["code"] for i in r["insights"]]
        self.assertIn("overconfidence", codes)
        d = r["insights"][0]["detail"]
        self.assertAlmostEqual(d["implied_confidence_pct"], 100.0)
        self.assertAlmostEqual(d["actual_acc_pct"], 25.0)


class Purity(unittest.TestCase):
    """§14.4 铁律:报表绝不写状态。"""

    def test_report_does_not_mutate(self):
        s, e, sid = fresh()
        feed(s, e, sid)
        before = snapshot(s)
        _ = report(s, "music")
        self.assertEqual(before, snapshot(s))

    def test_report_counts_domain_events(self):
        s, e, sid = fresh()
        feed(s, e, sid)
        r = report(s, "music")
        self.assertEqual(r["based_on_events"], 7)


if __name__ == "__main__":
    unittest.main()
