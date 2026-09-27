"""数据模型:SkillNode / SkillState / Question / Event / Session。

对应 SPEC.md §2 与 §3。字段即 schema,不做魔法。
"""
from dataclasses import dataclass, field
from typing import Optional


# ---- 常量(值域枚举) -------------------------------------------------
EVENT_TYPES = {
    "answer_correct", "answer_wrong",      # 已判分结果;payload 可含 retention: true
    "changed_answer",                      # 独立记录改答案(结算折半由 answer 事件 payload.changed_from 触发)
    "skip",                                # payload.reason: too_easy / too_hard / boring
    "pause",                               # 停留思考;不扣分,仅 last_seen
    "boredom_flag",                        # 用户显式喊无聊
    "followup",                            # 用户主动追问(最重要的兴趣信号)
    "self_report",                         # 用户自评(弱先验,file §8)
}
SKIP_REASONS = {"too_easy", "too_hard", "boring"}
QUESTION_KINDS = {"concept", "variant", "transfer"}
FORMATS = ("mcq", "free", "explain")
SOURCES = {"seed", "llm", "stub_llm", "user"}
QC_STATUS = {"draft", "approved"}


@dataclass
class SkillNode:
    id: str
    domain: str
    name: str
    parent: Optional[str] = None
    children: list = field(default_factory=list)
    prereqs: list = field(default_factory=list)


@dataclass
class SkillState:
    """认知地图的节点值(SPEC §2.3)。

    streak 字段 v1 不存历史:每次事件后按 log 重算(见 events.recompute_streaks),
    存表只是缓存。bias 是唯一有累积语义的字段。
    """
    node_id: str
    mastery: float = 50.0
    evidence_count: int = 0
    last_seen: int = 0
    correct_streak: int = 0
    wrong_streak: int = 0
    skip_streak: int = 0
    followup_streak: int = 0
    boredom_recent: int = 0
    difficulty_bias: int = 0

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id, "mastery": round(self.mastery, 3),
            "evidence_count": self.evidence_count, "last_seen": self.last_seen,
            "correct_streak": self.correct_streak, "wrong_streak": self.wrong_streak,
            "skip_streak": self.skip_streak, "followup_streak": self.followup_streak,
            "boredom_recent": self.boredom_recent, "difficulty_bias": self.difficulty_bias,
        }


@dataclass
class Question:
    id: str
    node_id: str
    difficulty: int                  # 0–100,与 mastery 同尺度
    format: str                      # mcq / free / explain
    kind: str = "concept"            # concept / variant / transfer(§14 迁移诊断依赖)
    body: dict = field(default_factory=dict)
    answer_key: dict = field(default_factory=dict)
    source: str = "seed"             # seed / llm / stub_llm / user
    qc_status: str = "approved"      # draft / approved
    served_at: int = 0               # 最近一次被出的时间(0 = 从未出过)
    times: int = 0                   # 被出过的次数


@dataclass
class Event:
    """只增不改。判分过程不进事件流,事件流只存已判分结果(SPEC §3)。"""
    ts: int
    session_id: str
    type: str
    question_id: Optional[str] = None
    node_id: Optional[str] = None
    difficulty: Optional[int] = None
    id: Optional[str] = None         # 可缺省,落库时自动生成
    payload: dict = field(default_factory=dict)


@dataclass
class Session:
    id: str
    domain: str
    dive_node: Optional[str] = None
    last_q_id: Optional[str] = None
    last_q_node: Optional[str] = None
    last_q_difficulty: Optional[int] = None
    created_ts: int = 0
