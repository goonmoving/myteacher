"""nudge 文案系统化(SPEC §7 / §9 / 验收 §11.7)。

所有"地图为什么变"的一句话反馈在此统一;events.py 只算不写文案。
规则:必须包含节点名 + 方向/结果 + 原因——用户地图要有体感。
本模块刻意独立(不 import events,避免循环):以下常量字面量
由 tests/test_nudges.py 与 events.py 常量对拍,防止漂移。
"""

CHANGED_WEIGHT = 0.5
SELF_REPORT_MAX_EVIDENCE = 3
GOAL_LO, GOAL_HI = 15, 95

_SKIP_NOTE = {"too_easy": "难度已上调", "boring": "该节点降权,换方向",
              "too_hard": "已记录太难", None: ""}


def _clip(x, lo=GOAL_LO, hi=GOAL_HI):
    return max(lo, min(hi, x))


def settle(node_name: str, obs: float, before: float, after: float,
           difficulty, changed: bool, bias: int) -> str:
    arrow = "↑" if after > before else ("↓" if after < before else "→")
    mark = "改答案折半记账;" if changed else ""
    label = "答对" if obs == 1 else "答错"
    goal = int(_clip(round(after + bias)))
    return (f"{node_name} {arrow} {before:.1f} → {after:.1f}"
            f"({mark}难度 {difficulty} {label});下次目标难度 → {goal}")


def skip(node_name: str, reason) -> str:
    note = _SKIP_NOTE.get(reason if reason is not None else None, "")
    return f"{node_name}:skip({reason or '未标注'}) {note}".strip()


def bored(node_name: str) -> str:
    return f"收到无聊信号:「{node_name}」降权,下一题换方向"


def changed(node_name: str, weight: float = CHANGED_WEIGHT) -> str:
    return f"已记录改答案:「{node_name}」;该题结算时打 {weight} 折"


def pause(node_name: str, dwell_ms) -> str:
    return f"{node_name}:停留 {dwell_ms if dwell_ms is not None else '-'}ms 思考,不扣分"


def followup(node_name: str, nth: int, entered: bool) -> str:
    msg = f"兴趣信号:第 {nth} 次追问"
    if entered:
        msg += f" → 进入深挖模式,接下来只围绕「{node_name}」出挑战题"
    return msg


def self_report(node_name: str, rating: float, before: float, moved,
                max_evidence: int = SELF_REPORT_MAX_EVIDENCE) -> str:
    if moved is not None:
        return f"{node_name}:自评 {rating:.0f} → 地图 {before:.1f} → {moved:.1f}"
    return f"{node_name}:自评已留存(证据 ≥ {max_evidence},真实事件主导)"
