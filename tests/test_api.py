"""M2 API 验收:端到端流程、LLM 隔离(§11.2)、QC(§6.3)、replay(§11.1)。"""
import unittest

from myteacher.api import Api
from myteacher.config import REPO_ROOT, bootstrap
from myteacher.model import Question
from myteacher.store import Store

T0 = 1_760_000_000


class FakeLLM:
    """可控假 LLM:合法出题;判分故意低置信 → 走用户自验(§6.1)。"""

    name = "fake"

    def __init__(self):
        self.n = 0
        self.judged = []

    def generate_question(self, node, target, fmt):
        self.n += 1
        return Question(id=f"q_fake_{node.id}_{self.n}", node_id=node.id,
                        difficulty=target, format=fmt, kind="concept",
                        body={"prompt": f"[fake] {node.name}?"},
                        answer_key={"answer": 0} if fmt == "mcq"
                        else {"rubric_points": ["要点"]},
                        source="fake", qc_status="draft")

    def judge(self, question, user_answer):
        self.judged.append((question.id, user_answer))
        return {"correct": True, "confidence": 0.5, "needs_user": True}

    def explain(self, question, user_answer, state):
        return f"[fake explain] {question.node_id}"

    def followup_reply(self, question, text, target):
        return "[fake followup reply]"


def make_api(llm=True):
    s = Store(":memory:")
    bootstrap(s, REPO_ROOT)
    if llm:
        fake = FakeLLM()
        return Api(s, llm=fake, now=lambda: T0), s, fake
    return Api(s, llm=None, now=lambda: T0), s, None


class ApiFlow(unittest.TestCase):
    def test_full_flow(self):
        api, s, _ = make_api()
        code, st = api.handle("POST", "/v1/sessions", {}, {"domain": "music"})
        self.assertEqual(code, 200)
        sid = st["session_id"]
        # ① 下一题
        code, c = api.handle("GET", "/v1/next", {"session": sid})
        self.assertEqual(code, 200)
        self.assertEqual(set(c), {"question", "node_id", "target_difficulty",
                                  "action_type", "reason", "dive_active"})
        q1 = c["question"]
        # ② 答对 → nudge + mastery
        code, r = api.handle("POST", "/v1/answers", {},
                             {"question_id": q1["id"], "session_id": sid,
                              "answer": 0, "dwell_ms": 3000,
                              "self_confidence": 4})
        self.assertEqual(code, 200)
        self.assertIn("nudge", r)
        self.assertIn("mastery", r)
        self.assertTrue(r["judge"]["correct"])
        # ③ 同题带 changed_from 答错(改答案后判错 → answer_wrong + 折半)
        code, r = api.handle("POST", "/v1/answers", {},
                             {"question_id": q1["id"], "session_id": sid,
                              "answer": 3, "changed_from": 2})
        self.assertEqual(code, 200)
        self.assertFalse(r["judge"]["correct"])
        self.assertIn("explanation", r)
        # ④ skip ×3 → 下一题偏航(§11.3)
        qid = sorted(s.candidate_questions("music.beat"),
                     key=lambda q_: q_.difficulty)[0].id
        for _ in range(3):
            code, _r = api.handle("POST", "/v1/skips", {},
                                  {"question_id": qid, "session_id": sid,
                                   "reason": "boring"})
            self.assertEqual(code, 200)
        code, nxt = api.handle("GET", "/v1/next", {"session": sid})
        self.assertEqual(code, 200)
        self.assertNotEqual(nxt["node_id"], "music.beat")
        # ⑤ followup ×2 → dive + 子树挑战题
        node, q2 = nxt["node_id"], nxt["question"]
        code, f1 = api.handle("POST", "/v1/followups", {},
                              {"question_id": q2["id"], "session_id": sid,
                               "text": "why"})
        self.assertEqual(code, 200)
        self.assertFalse(f1["dive_entered"])
        code, f2 = api.handle("POST", "/v1/followups", {},
                              {"question_id": q2["id"], "session_id": sid,
                               "text": "more"})
        self.assertEqual(code, 200)
        self.assertTrue(f2["dive_entered"])
        self.assertIn("next", f2)
        self.assertTrue(f2["next"]["dive_active"])
        from myteacher.events import Engine
        self.assertIn(f2["next"]["question"]["node_id"], Engine(s).subtree(node))
        # ⑥ map / ⑦ events 导出
        code, m = api.handle("GET", "/v1/map", {"domain": "music"})
        self.assertEqual(code, 200)
        self.assertGreaterEqual(len(m["nodes"]), 6)
        code, ex = api.handle("GET", "/v1/events", {"session": sid})
        self.assertEqual(code, 200)
        types = [e["type"] for e in ex["events"]]
        for want in ("followup", "skip", "answer_wrong", "answer_correct",
                     "changed_answer"):
            self.assertIn(want, types)
        # ⑧ replay(§11.1)与 live 状态一致
        code, rp = api.handle("GET", "/v1/replay", {"session": sid})
        self.assertEqual(code, 200)
        self.assertTrue(rp["matches_live"])

    def test_low_confidence_needs_user(self):
        api, s, _ = make_api()
        _, st = api.handle("POST", "/v1/sessions", {}, {"domain": "literature"})
        sid = st["session_id"]
        s.add_question(Question(id="q_free_1", node_id="literature.pov",
                                difficulty=50, format="free",
                                body={"prompt": "讲讲"},
                                answer_key={"rubric_points": ["视角"]}))
        code, r = api.handle("POST", "/v1/answers", {},
                             {"question_id": "q_free_1", "session_id": sid,
                              "answer": "视角就是谁在讲"})
        self.assertEqual(code, 409)
        self.assertTrue(r["judge"]["needs_user"])
        code, r = api.handle("POST", "/v1/answers", {},
                             {"question_id": "q_free_1", "session_id": sid,
                              "answer": "视角就是谁在讲", "confirm": False})
        self.assertEqual(code, 200)
        self.assertFalse(r["judge"]["correct"])

    def test_stub_cannot_draft_and_event_validation(self):
        api, s, _ = make_api(llm=False)
        code, r = api.handle("POST", "/v1/questions/drafts", {},
                             {"node_id": "music.beat"})
        self.assertEqual(code, 400)
        code, r = api.handle("POST", "/v1/events", {},
                             {"session_id": "s_none", "type": "answer_correct",
                              "node_id": "music.beat", "difficulty": 50})
        self.assertEqual(code, 400)

    def test_404s(self):
        api, s, _ = make_api()
        _, st = api.handle("POST", "/v1/sessions", {}, {"domain": "music"})
        code, _r = api.handle("POST", "/v1/answers", {},
                              {"question_id": "q_none", "session_id": st["session_id"],
                               "answer": 1})
        self.assertEqual(code, 404)
        code, _r = api.handle("POST", "/v1/sessions", {}, {"domain": "nope"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
