"""§11.3–5 策略验收:反无聊 / 深挖 / 回退 / 难度偏差。"""
import unittest

from myteacher.config import REPO_ROOT, bootstrap
from myteacher.events import Engine
from myteacher.model import Event
from myteacher.policy import choose_next
from myteacher.store import Store

T0 = 1_760_000_000
DAY = 86_400


def fresh():
    s = Store(":memory:")
    bootstrap(s, REPO_ROOT)
    return s


def session(store, domain="music"):
    eng = Engine(store, now=lambda: T0)
    return eng.create_session(domain), eng


def ev(sid, ts, typ, node, q=None, diff=None, payload=None):
    return Event(ts=ts, session_id=sid, type=typ, question_id=q.id if q else None,
                 node_id=node, difficulty=diff, payload=payload or {})


class AntiBoredom(unittest.TestCase):
    """§11.3 最近 5 事件 skip≥3 → 下一题必换节点或难度变化 ≥15。"""

    def test_skip3_forces_divert(self):
        s = fresh()
        sid, eng = session(s)
        c1 = choose_next(s, sid, T0)
        node, q = c1.node_id, c1.question
        for i in range(3):
            eng.apply_event(ev(sid, T0 + 10 + i, "skip", node, q,
                               payload={"reason": "boring"}))
        c2 = choose_next(s, sid, T0 + 3600)
        ok = (c2.node_id != node
              or abs(c2.target_difficulty - q.difficulty) >= 15)
        self.assertTrue(ok, f"反无聊偏航失败:{c2}")
        self.assertFalse(c2.dive_active)


class DiveMode(unittest.TestCase):
    """§11.4 同 session 同节点 followup≥2 → dive 激活,出题限子树。"""

    def test_double_followup_enters_dive(self):
        s = fresh()
        sid, eng = session(s)
        c1 = choose_next(s, sid, T0)
        node, q = c1.node_id, c1.question
        r1 = eng.apply_event(ev(sid, T0 + 100, "followup", node, q,
                                payload={"text": "why?"}))
        self.assertFalse(r1["dive_entered"])
        r2 = eng.apply_event(ev(sid, T0 + 101, "followup", node, q,
                                payload={"text": "more"}))
        self.assertTrue(r2["dive_entered"])

        c2 = choose_next(s, sid, T0 + 120)
        self.assertTrue(c2.dive_active)
        sub = Engine(s).subtree(node)
        self.assertIn(c2.node_id, sub)
        # 挑战难度:目标 = mastery + 20(clamp 15..95)
        st = s.state(node)
        want = max(15, min(95, round(st.mastery + 20)))
        self.assertLessEqual(c2.question.difficulty, want)

    def test_boredom_exits_dive(self):
        s = fresh()
        sid, eng = session(s)
        c1 = choose_next(s, sid, T0)
        node, q = c1.node_id, c1.question
        for i in range(2):
            eng.apply_event(ev(sid, T0 + 100 + i, "followup", node, q))
        eng.apply_event(ev(sid, T0 + 200, "boredom_flag", node, q))
        c2 = choose_next(s, sid, T0 + 210)
        self.assertFalse(c2.dive_active)


class Rescue(unittest.TestCase):
    """§11.5 wrong_streak≥2 的固定输入 → 下一题不再落该节点(父/低 mastery 兄弟)。"""

    def test_two_wrongs_change_node(self):
        s = fresh()
        sid, eng = session(s)
        node = "music.beat"
        qs = sorted(s.candidate_questions(node), key=lambda q_: q_.difficulty)
        q = qs[0]
        eng.apply_event(ev(sid, T0 + 10, "answer_wrong", node, q, q.difficulty))
        eng.apply_event(ev(sid, T0 + 20, "answer_wrong", node, q, q.difficulty))
        c = choose_next(s, sid, T0 + 40)
        self.assertNotEqual(c.node_id, node, f"回退失败:{c}")
        self.assertIn(c.action_type, ("rescue", "learn"))


class Bias(unittest.TestCase):
    def test_streak3_raises_bias(self):
        s = fresh()
        sid, eng = session(s)
        node = "music.orchestration"
        qs = sorted(s.candidate_questions(node), key=lambda q_: abs(q_.difficulty - 48))
        q = qs[0]
        for i in range(3):
            eng.apply_event(ev(sid, T0 + 10 + i, "answer_correct", node, q,
                               q.difficulty))
        self.assertEqual(s.state(node).difficulty_bias, 15)

    def test_wrong2_lowers_bias(self):
        s = fresh()
        sid, eng = session(s)
        node = "music.melody_expectation"
        qs = sorted(s.candidate_questions(node), key=lambda q_: q_.difficulty)
        q = qs[0]
        eng.apply_event(ev(sid, T0 + 10, "answer_wrong", node, q, q.difficulty))
        eng.apply_event(ev(sid, T0 + 20, "answer_wrong", node, q, q.difficulty))
        self.assertEqual(s.state(node).difficulty_bias, -10)


if __name__ == "__main__":
    unittest.main()
