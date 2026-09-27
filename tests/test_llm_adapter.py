"""LLM 适配层验收:白名单清洗(SPEC §6.3)、网络重试、JSON 解析。"""
import json
import unittest
from unittest.mock import patch

from myteacher.config import REPO_ROOT, bootstrap
from myteacher.llm import (LLMError, OpenAICompatLLM, StubLLM,
                           sanitize_question)
from myteacher.model import SkillNode
from myteacher.store import Store

NODE = SkillNode(id="perspective.dynamic_pose", domain="perspective",
                 name="Dynamic Pose", parent="perspective")


class Sanitize(unittest.TestCase):
    def test_valid_mcq_passes(self):
        raw = {"node_id": "someone.elses.node",          # LLM 想挪归属 → 强制覆盖
               "mastery": 0,                             # 恶意:想顺带改状态 → 剥离
               "difficulty": 62, "format": "mcq", "kind": "variant",
               "body": {"prompt": "P?", "options": ["a", "b"]},
               "answer_key": {"answer": 1}}
        q = sanitize_question(raw, NODE, 60, "mcq")
        self.assertEqual(q.node_id, NODE.id)          # 白名单:node_id 不可变
        self.assertEqual(q.qc_status, "draft")
        self.assertEqual(q.kind, "variant")
        self.assertFalse(hasattr(q, "mastery"))       # 字段级隔离
        self.assertEqual(q.difficulty, 62)

    def test_mcq_missing_options_rejected(self):
        raw = {"difficulty": 60, "format": "mcq",
               "body": {"prompt": "P?"}, "answer_key": {"answer": 0}}
        with self.assertRaises(LLMError):
            sanitize_question(raw, NODE, 60, "mcq")

    def test_free_missing_rubric_rejected(self):
        raw = {"format": "free", "body": {"prompt": "P?"}, "answer_key": {}}
        with self.assertRaises(LLMError):
            sanitize_question(raw, NODE, 60, "free")

    def test_difficulty_clamped(self):
        raw = {"difficulty": 999, "format": "mcq",
               "body": {"prompt": "P?", "options": ["a", "b"]},
               "answer_key": {"answer": 0}}
        self.assertEqual(sanitize_question(raw, NODE, 60, "mcq").difficulty, 95)


class AdapterHTTP(unittest.TestCase):
    def make(self):
        return OpenAICompatLLM("http://endpoint.test/v1", "key-x", "test-model")

    def test_parse_json_tolerates_fences(self):
        self.assertEqual(OpenAICompatLLM._parse_json(
            '```json\n{"a": 1}\n```'), {"a": 1})
        with self.assertRaises(LLMError):
            OpenAICompatLLM._parse_json("not json at all")

    def test_chat_retries_then_parses(self):
        llm = self.make()
        calls = []

        class R:
            def __enter__(self):
                return self

        llm = self.make()
        calls = []
        attempt = {"n": 0}

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                calls.append(1)
                return json.dumps(
                    {"choices": [{"message": {"content": '{"ok": 1}'}}]}).encode()

        def fake_urlopen(req, timeout=None):
            attempt["n"] += 1
            if attempt["n"] == 1:
                raise ConnectionError("first attempt fails")
            return R()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            out = llm._parse_json(llm._chat([{"role": "user", "content": "gimme"}]))
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(len(calls), 1)
        self.assertEqual(attempt["n"], 2)
        self.assertEqual(len(calls), 1)


class StubBehavior(unittest.TestCase):
    def test_stub_judge_low_confidence(self):
        s = Store(":memory:")
        bootstrap(s, REPO_ROOT)
        llm = StubLLM()
        q = llm.generate_question(s.node("music.beat"), 55, "explain")
        self.assertEqual(q.qc_status, "draft")
        self.assertTrue(q.answer_key.get("rubric_points"))


if __name__ == "__main__":
    unittest.main()
