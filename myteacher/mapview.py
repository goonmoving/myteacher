"""认知地图终端渲染(输入 = GET /v1/map 的 payload)。

规则(SPEC §12 M3):
- 树形缩进;每节点一个 10 格掌握度条 ▮/▯ + 百分数
- 证据 <3 标「估」;<40 标「▼低洼」;>85 标「▲」
- 输出确定性(同 payload 同输出),方便对拍测试
"""
BAR_F, BAR_E, BAR_SLOTS = "▮", "▯", 10


def bar_str(mastery: float) -> str:
    filled = int(round(mastery / 100.0 * BAR_SLOTS))
    filled = max(0, min(BAR_SLOTS, filled))
    return f"{BAR_F * filled}{BAR_E * (BAR_SLOTS - filled)}"


def render_map(map_payload: dict) -> str:
    nodes = map_payload["nodes"]
    by_parent = {}
    for n in nodes:
        by_parent.setdefault(n["parent"], []).append(n)
    out = []
    legend_done = False

    def draw(pid, depth):
        for n in sorted(by_parent.get(pid, []), key=lambda x: x["id"]):
            st = n["state"]
            tags = []
            if st["evidence_count"] < 3:
                tags.append("估")
            if st["mastery"] < 40:
                tags.append("▼低洼")
            elif st["mastery"] > 85:
                tags.append("▲")
            line = (f"{'  ' * depth}{bar_str(st['mastery'])} {st['mastery']:5.1f}%"
                    f"  {n['name']}")
            if tags:
                line += f"  [{' ·'.join(tags)}]"
            out.append(line)
            draw(n["id"], depth + 1)

    draw(None, 0)
    out.append("-" * 44)
    out.append("▮=掌握度 ▼低洼(<40) ▲精通(>85) [估]=证据<3")
    return "\n".join(out)
