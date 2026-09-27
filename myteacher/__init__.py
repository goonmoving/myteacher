"""myteacher — 学习状态模型(M1 骨架)

架构原则(见 SPEC.md):
- 地图(SkillState)是唯一事实源,由事件流推导,可重放
- 策略函数确定性决策(Next Best Action)
- LLM 只出题/判分/解释,永不改地图
"""
from myteacher.model import (
    Event, Question, Session, SkillNode, SkillState,
)
from myteacher.events import Engine
from myteacher.policy import choose_next, NextChoice
from myteacher.config import bootstrap, load_domains

__all__ = [
    "Event", "Question", "Session", "SkillNode", "SkillState",
    "Engine", "choose_next", "NextChoice", "bootstrap", "load_domains",
]
__version__ = "0.1.0"
