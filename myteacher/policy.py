"""下一题策略(Next Best Action,SPEC §5)。

确定性:输入 = 地图 + 近期事件;LLM 不参与决策。
输出 = Question + action_type(learn/review/variant/transfer/rescue)+ reason。
"""
from dataclasses import dataclass
from typing import Optional

from myteacher.events import SECONDS_PER_DAY, Engine, clip
from myteacher.model import Question

ZLOW, ZHIGH = 40, 90           # 作战区(SPEC §5.1)
CHALLENGE_DELTA = 20           # 深挖模式难度偏移
REVIEW_DAYS = 7                # ≥7 天未见 → review
FMT_CYCLE = ("mcq", "free", "explain")
REPEAT_COOLDOWN_DAYS = 14      # 同一题 14 天内不重复出(SPEC §5.4)

W_REC, W_ZONE, W_BELOW, W_INTR, W_BORE = 20, 20, 25, 15, 30
MIN_SKIP_FOR_OVERRIDE = 3      # §11.3:最近 5 事件 skip≥3 → 换题偏航
DIFF_CHANGE_MIN = 15


@dataclass
class NextChoice:
    question: Question
    node_id: str
    target_difficulty: int
    action_type: str            # learn / review / variant / transfer / rescue
    reason: str                 # 为什么是这道题(给人看)
    dive_active: bool

@dataclass
class Weights:
    """§5.1 评分权重(可注入;默认=模块常量,tune.py 网格搜索用)。"""
    rec: float = W_REC
    zone: float = W_ZONE
    below: float = W_BELOW
    intr: float = W_INTR
    bore: float = W_BORE


DEFAULT_WEIGHTS = Weights()


def choose_next(store, session_id: str, now_ts: int, llm=None,
                weights=None) -> NextChoice:
    w = weights or DEFAULT_WEIGHTS
    sess = store.session(session_id)
    if sess is None:
        raise ValueError(f"unknown session: {session_id}")
    all_nodes = store.nodes(sess.domain)
    root_ids = {n.id for n in all_nodes if not n.parent}
    candidates = [n for n in all_nodes if n.parent] or all_nodes

    dive_active = sess.dive_node is not None
    if dive_active:
        sub = Engine(store).subtree(sess.dive_node)
        sub_candidates = [n for n in candidates if n.id in sub]
        candidates = sub_candidates or candidates

    # 上一题信息(§5.4 防重复 / §11.3 反无聊)
    prev_node = sess.last_q_node
    prev_diff = sess.last_q_difficulty
    prev_qid = sess.last_q_id
    last5 = store.events(session_id=session_id, ascending=False, limit=5)
    skip5 = sum(1 for e in last5 if e.type == "skip")

    def parent_mastery(n) -> float:
        """父(可为根)的聚合掌握度,用于 gap_bonus 的“父节点已过半”判断。"""
        if not n.parent:
            return 50.0
        p = store.node(n.parent)
        if p is None:
            return 50.0
        if p.parent:
            return store.state(p.id).mastery
        kids = [k for k in all_nodes if k.parent == p.id]
        if not kids:
            return 50.0
        return sum(store.state(k.id).mastery for k in kids) / len(kids)

    min_mastery = min((store.state(n.id).mastery for n in candidates), default=50.0)
    min_node_ids = {n.id for n in candidates
                    if abs(store.state(n.id).mastery - min_mastery) < 1e-6}

    # ---- §5.1 节点评分 ----
    scored = []
    for n in candidates:
        st = store.state(n.id)
        # 排除:刚问过的节点(除非深挖指向它,或它是全场最低洼)
        if (not dive_active and prev_node == n.id
                and n.id not in min_node_ids
                and st.wrong_streak < 2):     # 连错节点优先回退候补,免重新拔最高
            continue
        days_idle = ((now_ts - st.last_seen) / SECONDS_PER_DAY if st.last_seen else 999.0)
        rec = clip(days_idle / 30.0, 0.0, 1.0) if st.last_seen else 1.0
        zone = 1.0 if (ZLOW <= st.mastery <= ZHIGH) else 0.6
        gap = 1.0 - st.mastery / 100.0
        if parent_mastery(n) >= 60:
            gap = min(gap + 0.25, 1.25)
        intr = min(st.followup_streak, 3)
        bore = st.skip_streak + st.boredom_recent
        score = (w.rec * rec + w.zone * zone + w.below * gap
                 + w.intr * intr - w.bore * bore)
        scored.append((score, n))
    if not scored:
        # 全被排除(单节点领域/上一题即唯一低洼):兜底允许复问
        for n in candidates:
            st = store.state(n.id)
            days_idle = ((now_ts - st.last_seen) / SECONDS_PER_DAY if st.last_seen else 999.0)
            rec = clip(days_idle / 30.0, 0.0, 1.0) if st.last_seen else 1.0
            scored.append((w.rec * rec, n))
    scored.sort(key=lambda t: (-t[0], t[1].id))

    chosen = scored[0][1]

    # ---- 回退(§11.5):连续错 2 题的节点不再继续,退父/兄弟 ----
    rescue = False
    if (not dive_active and store.state(chosen.id).wrong_streak >= 2
            and chosen.id not in min_node_ids):
        alt = [(s, n) for (s, n) in scored
               if n.id != chosen.id and store.state(n.id).wrong_streak < 2]
        if alt:
            chosen = alt[0][1]
            rescue = True

    # ---- §11.3 反无聊偏航 ----
    if skip5 >= MIN_SKIP_FOR_OVERRIDE and prev_node is not None:
        def differs(n):
            return abs(_target_for(store, n, dive_active)
                       - (prev_diff if prev_diff is not None else 0)) >= DIFF_CHANGE_MIN
        okay = [(s, n) for (s, n) in scored if n.id != prev_node or differs(n)]
        if okay:
            chosen = okay[0][1]

    st = store.state(chosen.id)
    target = _target_for(store, chosen, dive_active)
    action_type = _action_type(store, chosen, target, now_ts, dive_active, rescue)

    # 题型轮换:按该节点累计出题次数轮转;深挖偏 explain
    if dive_active:
        fmt = "explain"
    else:
        idx = _served_count(store, chosen.id) % len(FMT_CYCLE)
        prev_q = store.question(prev_qid) if prev_qid else None
        if prev_q and FMT_CYCLE[idx] == prev_q.format:
            idx = (idx + 1) % len(FMT_CYCLE)
        fmt = FMT_CYCLE[idx]

    q = _pick_question(store, chosen.id, target, fmt,
                       prev_qid=prev_qid, now_ts=now_ts)
    if q is None and llm is not None:
        q = llm.generate_question(chosen, target, fmt)
        if q is not None:
            store.add_question(q)
    if q is None:
        raise RuntimeError(f"no question available for node {chosen.id}")

    if q.kind == "variant" and action_type in ("learn", "review"):
        action_type = "variant"
    elif q.kind == "transfer" and action_type in ("learn", "review"):
        action_type = "transfer"

    sess = store.session(session_id)
    sess.last_q_id = q.id
    sess.last_q_node = chosen.id
    sess.last_q_difficulty = q.difficulty
    store.update_session(sess)
    store.mark_served(q.id, now_ts)

    return NextChoice(question=q, node_id=chosen.id, target_difficulty=q.difficulty,
                      action_type=action_type,
                      reason=_reason(action_type, q, chosen, store, now_ts, dive_active),
                      dive_active=dive_active)


def _action_type(store, node, target: int, now_ts: int, dive_active: bool,
                 rescue: bool) -> str:
    if rescue:
        return "rescue"
    st = store.state(node.id)
    if (not dive_active and st.last_seen
            and (now_ts - st.last_seen) >= REVIEW_DAYS * SECONDS_PER_DAY):
        return "review"
    return "learn"


def _target_for(store, node, dive_active: bool) -> int:
    st = store.state(node.id)
    delta = CHALLENGE_DELTA if dive_active else 0
    return int(clip(round(st.mastery + st.difficulty_bias + delta), 15, 95))


def _served_count(store, node_id: str) -> int:
    return sum(q.times for q in store.candidate_questions(node_id))


def _pick_question(store, node_id: str, target: int, fmt: str,
                   prev_qid: Optional[str], now_ts: int):
    cands = [q for q in store.candidate_questions(node_id)
             if q.qc_status == "approved" or q.served_at == 0]
    window_start = now_ts - REPEAT_COOLDOWN_DAYS * SECONDS_PER_DAY
    cands = [q for q in cands
             if q.id != prev_qid and not (0 < q.served_at <= window_start)] or cands
    same_fmt = [q for q in cands if q.format == fmt] or cands
    same_fmt.sort(key=lambda q: (abs(q.difficulty - target), q.id))
    return same_fmt[0] if same_fmt else None


_ACTION_LABEL = {"learn": "学新", "review": "复习", "variant": "变式",
                 "transfer": "迁移", "rescue": "回退"}


def _reason(action_type, q, node, store, now_ts, dive_active) -> str:
    st = store.state(node.id)
    label = _ACTION_LABEL.get(action_type, action_type)
    if dive_active:
        why = "深挖模式:围绕该技能的挑战题"
    elif action_type == "review":
        days = max(1, int((now_ts - st.last_seen) / SECONDS_PER_DAY))
        why = f"距上次 {days} 天,先检验保持"
    elif action_type == "rescue":
        why = f"上一技能连续答错,回退一步(目标难度 {q.difficulty})"
    else:
        why = f"「{node.name}」mastery {st.mastery:.0f}%,目标难度 {q.difficulty}"
    return f"{label}(难度 {q.difficulty}):{why}"
