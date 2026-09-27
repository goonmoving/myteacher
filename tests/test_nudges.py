"""nudge 文案系统化验收(§11.7 每反馈含节点名+方向+原因)+ 常量对拍。"""
import unittest

from myteacher import events, nudges


class ConstantsSync(unittest.TestCase):
    """nudges.py 刻意不 import events(防循环);字面量必须由测试对拍。"""

    def test_weights_match(self):
        self.assertEqual(nudges.CHANGED_WEIGHT, events.CHANGED_WEIGHT)
        self.assertEqual(nudges.SELF_REPORT_MAX_EVIDENCE,
                         events.SELF_REPORT_MAX_EVIDENCE)


class Copy(unittest.TestCase):
    def test_settle_has_name_direction_reason(self):
        up = nudges.settle("节拍", 1.0, 50.0, 62.0, 40, False, 0)
        self.assertIn("节拍", up)
        self.assertIn("↑", up)
        self.assertIn("50.0 → 62.0", up)
        self.assertIn("答对", up)
        dn = nudges.settle("节拍", 0.0, 50.0, 34.0, 55, True, 0)
        self.assertIn("↓", dn)
        self.assertIn("改答案折半记账", dn)

    def test_goal_clamped(self):
        # after 95 + bias 20 → clamp 95
        self.assertIn("95", nudges.settle("x", 1.0, 60, 95.0, 90, False, 20))

    def test_skip_copy(self):
        self.assertIn("难度已上调", nudges.skip("节奏", "too_easy"))
        self.assertIn("降权", nudges.skip("节奏", "boring"))
        d = nudges.skip("节奏", None)
        self.assertIn("未标注", d)

    def test_bored_and_changed_and_pause(self):
        self.assertIn("降权", nudges.bored("文学·意象"))
        self.assertIn("0.5 折", nudges.changed("配器"))
        self.assertIn("不扣分", nudges.pause("节拍", 42000))
        self.assertIn("-", nudges.pause("节拍", None))

    def test_followup_entered_flag(self):
        self.assertNotIn("深挖", nudges.followup("配器", 1, False))
        got = nudges.followup("配器", 2, True)
        self.assertIn("深挖", got)
        self.assertIn("配器", got)

    def test_self_report_moved_or_kept(self):
        moved = nudges.self_report("节拍", 80, 50.0, 59.0)
        self.assertIn("50.0 → 59.0", moved)
        kept = nudges.self_report("节拍", 80, 50.0, None)
        self.assertIn("真实事件主导", kept)


if __name__ == "__main__":
    unittest.main()
