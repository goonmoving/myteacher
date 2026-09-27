"""终端认知地图渲染 + 聊天壳脚本驱动(SPEC §12 M3)。"""
import unittest

from myteacher.api import Api
from myteacher.chat import Chat
from myteacher.config import REPO_ROOT, bootstrap
from myteacher.mapview import bar_str, render_map
from myteacher.store import Store


def map_payload():
    s = Store(":memory:")
    bootstrap(s, REPO_ROOT)
    api = Api(s, llm=None, now=lambda: 1_760_000_000)
    code, m = api.handle("GET", "/v1/map", {"domain": "music"})
    assert code == 200, m
    return m


class MapView(unittest.TestCase):
    def test_deterministic_render(self):
        self.assertEqual(render_map(map_payload()), render_map(map_payload()))

    def test_contains_marks_and_bars(self):
        out = render_map(map_payload())
        self.assertIn("音乐", out)
        self.assertIn("配器", out)
        # 全新地图:根+5 叶证据全 0 → 六个节点行 + 图例的「估」
        self.assertGreaterEqual(out.count("[估]"), 6)
        self.assertIn("▼低洼", out)      # 图例
        self.assertIn("▮▮▮▮▮▯", out)     # 50% → 前 5 格填充

    def test_bar_str(self):
        self.assertEqual(bar_str(50.0), "▮▮▮▮▮▯▯▯▯▯")
        self.assertEqual(bar_str(100.0), "▮▮▮▮▮▮▮▮▮▮")
        self.assertEqual(bar_str(0.0), "▯▯▯▯▯▯▯▯▯▯")


class ChatScript(unittest.TestCase):
    """脚本驱动 Chat.step;应能跑完答题/跳过/追问/深挖/校准全流程。"""

    def setUp(self):
        s = Store(":memory:")
        bootstrap(s, REPO_ROOT)
        self.api = Api(s, llm=None, now=lambda: 1_760_000_000)
        self.outputs = []

    def out(self):
        return "\n".join(self.outputs)

    def chat(self):
        c = Chat(self.api, "music", out=self.outputs.append,
                 ask=lambda *a: "n")
        c.start()
        return c

    def test_map_command(self):
        c = self.chat()
        self.outputs.clear()
        self.assertTrue(c.step("map"))
        self.assertIn("音乐", self.out())
        self.assertIn("[估]", self.out())

    def test_wrong_answer_then_why(self):
        c = self.chat()
        self.outputs.clear()
        self.assertTrue(c.step("B"))          # 首题(beat@40)选 B → 错
        self.assertIn("mastery=", self.out())
        self.outputs.clear()
        self.assertTrue(c.step("why"))        # 无 LLM → 回显讲评或暂无来源
        selfoutputs = self.out()
        self.assertTrue(isinstance(selfoutputs, str))

    def test_skip_too_easy_raises(self):
        c = self.chat()
        self.outputs.clear()
        self.assertTrue(c.step("skip too_easy"))
        self.assertIn("难度已上调", self.out())

    def test_followup_dive_flow(self):
        c = self.chat()
        self.assertTrue(c.step("followup 为什么"))
        self.assertFalse(c.step("followup 再深入") is False)
        self.assertIn("深挖", "\n".join(self.outputs))
        self.assertTrue(c.step("map"))        # 深挖状态下地图仍可用
        self.assertIn("[估]", self.out())

    def test_calibrate_and_exit(self):
        c = self.chat()
        self.assertTrue(c.step("calibrate 90"))
        self.assertIn("自评", self.out())
        self.assertFalse(c.step("exit"))


if __name__ == "__main__":
    unittest.main()
