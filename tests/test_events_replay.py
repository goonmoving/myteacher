"""§11.1 重放一致性 + §11.2 LLM 隔离 + 事件细节(折半/自评/快照字段)。"""
import unittest

from myteacher.config import bootstrap, REPO_ROOT
from myteacher.events import Engine, expected, k_factor
from myteacher.model import Event, Question
from myteacher.policy import choose_next
from myteacher.replay import snapshot, event_log, load_log
from myteacher.store import Store

T0 = 1_760_000_000  # 固定时钟基准


def fresh(repo_root=None):
    s = Store(":memory:")
    bootstrap(s, repo_root or REPO_ROOT)
    return s


class ReplayConsistency(unittest.TestCase):
    """§11.1 同一事件序列重放 → 地图逐节点相同(含 streak、bias、dive)。"""

    def test_replay_matches(self):
        s1 = fresh()
        e1 = Engine(s1, now=lambda: T0)
        sid = e1.create_session("music")
        sid2 = e1.create_session("literature")

        def ev(ts, t, nid, qid=None, diff=None, payload=None):
            return Event(ts=ts, session_id=sid, type=t, question_id=qid,
                         node_id=nid, difficulty=diff, payload=payload or {})

        seq = [
            ev(T0 + 1, "answer_wrong", "music.harmony_function", diff=55),
            ev(T0 + 2, "answer_correct", "music.harmony_function", diff=45,
               payload={"changed_from": 2}),
            ev(T0 + 3, "answer_correct", "music.harmony_function", diff=45),
            ev(T0 + 4, "skip", "music.rhythm", payload={"reason": "boring"}),
            ev(T0 + 5, "boredom_flag", "music.rhythm"),
            ev(T0 + 6, "pause", "music.beat", payload={"dwell_ms": 42000}),
            ev(T0 + 7, "followup", "music.orchestration"),
            ev(T0 + 8, "followup", "music.orchestration"),
            ev(T0 + 9, "changed_answer", "music.orchestration"),
            ev(T0 + 10, "answer_correct", "music.orchestration", diff=50),
            ev(T0 + 11, "self_report", "music.beat",
               payload={"self_rating": 88}),
            Event(ts=T0 + 12, session_id=sid2, type="answer_wrong",
                  node_id="literature.lang_rhythm", difficulty=60),
        ]
        for e in seq:
            e1.apply_event(e)
        snap1 = snapshot(s1)
        log = event_log(s1)

        s2 = fresh()
        from myteacher.model import Session
        load_log(s2, log, sessions=[Session(id=sid, domain="music", created_ts=T0),
                                    Session(id=sid2, domain="literature", created_ts=T0)])
        self.assertEqual(snap1, snapshot(s2))


class LLMIsolation(unittest.TestCase):
    """§11.2 地图状态只受事件管线影响;未知事件类型被拒且不产生副作用。"""

    def test_unknown_event_type_rejected(self):
        s = fresh()
        e = Engine(s, now=lambda: T0)
        sid = e.create_session("music")
        bad = Event(ts=T0, session_id=sid, type="set_all_mastery_zero",
                    node_id="music.beat", payload={"mastery": 0})
        with self.assertRaises(ValueError):
            e.apply_event(bad)
        self.assertEqual(s.state("music.beat").mastery, 50.0)
        self.assertEqual(len(s.all_events()), 0)

    def test_out_of_range_masters_rejected_by_validation(self):
        s = fresh()
        e = Engine(s, now=lambda: T0)
        sid = e.create_session("music")
        bad = Event(ts=T0, session_id=sid, type="answer_correct",
                    node_id="no_such_node", difficulty=50)
        with self.assertRaises(ValueError):
            e.apply_event(bad)
        self.assertEqual(len(s.all_events()), 0)


class Settlement(unittest.TestCase):
    """§4.1 更新公式、折半记账、自评权重、§11.6/7/8。"""

    def test_first_wrong_moves_down(self):
        s = fresh()
        e = Engine(s, now=lambda: T0)
        sid = e.create_session("music")
        r = e.apply_event(Event(ts=T0, session_id=sid, type="answer_wrong",
                                node_id="music.harmony_function", difficulty=55))
        exp = expected(50.0, 55)
        before = 50.0
        want = before + k_factor(0) * (0.0 - exp)   # = 40 * -0.4 = -16
        self.assertAlmostEqual(r["mastery_after"], want, places=4)
        self.assertIn("↓", r["nudge"])

    def test_changed_answer_halves_ladder(self):
        for changed in (False, True):
            s = fresh()
            e = Engine(s, now=lambda: T0)
            sid = e.create_session("music")
            payload = {"changed_from": 3} if changed else {}
            r = e.apply_event(Event(ts=T0, session_id=sid, type="answer_wrong",
                                    node_id="music.harmony_function", difficulty=55,
                                    payload=payload))
            if changed:
                self.assertAlmostEqual(r["mastery_after"], 50 - 16 * 0.5, places=4)
            else:
                self.assertAlmostEqual(r["mastery_after"], 50 - 16.0, places=4)

    def test_self_report_weak_prior(self):
        s = fresh()
        e = Engine(s, now=lambda: T0)
        sid = e.create_session("music")
        r = e.apply_event(Event(ts=T0, session_id=sid, type="self_report",
                                node_id="music.beat", payload={"self_rating": 80}))
        # 证据 0 <3 → 向自评靠 30%:50 → 59
        self.assertAlmostEqual(r["mastery_after"], 59.0, places=4)

    def test_nudge_and_snapshot_fields(self):
        s = fresh()
        e = Engine(s, now=lambda: T0)
        sid = e.create_session("music")
        r = e.apply_event(Event(ts=T0, session_id=sid, type="answer_correct",
                                node_id="music.orchestration", difficulty=50,
                                payload={"self_confidence": 3}))
        self.assertIn("nudge", r)
        self.assertIn("mastery_after", r)
        log = event_log(s)
        self.assertEqual(log[-1]["payload"].get("self_confidence"), 3)
        self.assertIn("mastery_after", log[-1]["payload"])


if __name__ == "__main__":
    unittest.main()
