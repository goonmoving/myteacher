"""M4 tune harness 验收:合成回路确定性 + 网格输出形状。"""
import unittest

from myteacher.tune import CANDIDATES, grid, simulate, score


class Simulation(unittest.TestCase):
    def test_deterministic(self):
        a = simulate("music", CANDIDATES["base"], rounds=60, seed=3)
        b = simulate("music", CANDIDATES["base"], rounds=60, seed=3)
        self.assertEqual(a, b)

    def test_metrics_shape(self):
        m = simulate("music", CANDIDATES["base"], rounds=60, seed=3)
        self.assertEqual(m["coverage"], 1.0)     # 60 轮足以覆盖全部叶节点
        self.assertIn("map_err", m)
        self.assertIn("goal_err", m)
        self.assertIn("bore_ok", m)
        self.assertGreaterEqual(m["bore_ok"], 0.0)
        self.assertLessEqual(m["map_err"], 100.0)
        self.assertEqual(m["weights"]["rec"], 20)

    def test_grid_sorted_by_score(self):
        rows = grid("music", rounds=40, seed=1)
        self.assertEqual(len(rows), len(CANDIDATES))
        scores = [r["score"] for r in rows]
        self.assertEqual(scores, sorted(scores))


if __name__ == "__main__":
    unittest.main()
