"""事件管线:地图的唯一写入者(SPEC §3 / §4)。

任何组件(LLM、策略、API、CLI)想改变地图,只能产生事件走这里。
未知事件类型一律拒绝——这是 §11.2 LLM 隔离的落地点。
"""
import math
import time
import uuid

from myteacher.model import Event
from myteacher import nudges

SECONDS_PER_DAY = 86400

RECENT_WINDOW = 5          # streak 只看最近 N=5 条相关事件(SPEC §2.3)
K0 = 40.0                  # 首次检验的最大学习步长
CHANGED_WEIGHT = 0.5       # 改答案结算打 0.5 折(SPEC §3.1 / §9)
SELF_REPORT_WEIGHT = 0.3   # 低证据期自评权重(SPEC §8.2)
SELF_REPORT_MAX_EVIDENCE = 3

BIAS_ON_STREAK3 = 15       # 连对 3 题 → 难度上调
BIAS_ON_WRONG2 = -10       # 连错 2 题 → 难度下调
BIAS_ON_TOO_EASY = 15
BIAS_ON_BORING = 10

DIVE_ENTER_N = 2           # 同 session 同节点连续 ≥2 次追问 → 深挖(SPEC §5.3)
DIVE_EXIT_SKIP_RUN = 3     # 深挖中连续 3 次跳过 → 退出

ANSWER_FAMILY = ("answer_correct", "answer_wrong", "retention_check")
ALL_EVENT_TYPES = {
    "answer_correct", "answer_wrong", "changed_answer", "skip", "pause",
    "boredom_flag", "followup", "retention_check", "self_report",
}


def clip(x, lo, hi):
    return max(lo, min(hi, x))


def expected(mastery: float, difficulty: float) -> float:
    """这题“本来该不该答对”的朴素预期(SPEC §4.1)。"""
    return clip((mastery - difficulty) / 50.0 + 0.5, 0.05, 0.95)


def k_factor(evidence_count: int) -> float:
    """学习步长随证据量衰减:新节点快校准,老节点稳定。"""
    return K0 / (1.0 + math.log2(1.0 + max(0, evidence_count)))


def recompute_streaks(store, node_id: str) -> dict:
    """从 store 里该节点最近 5 条事件扫出各 streak(v1 不存历史)。"""
    evs = store.events(node_id=node_id, ascending=False, limit=RECENT_WINDOW)
    cs = ws = ss = fs = 0
    cs_done = ws_done = ss_done = fs_done = False
    boredom_recent = 0
    for e in evs:
        t = e.type
        if t == "boredom_flag":
            boredom_recent = 1
        if not cs_done:
            if t == "answer_correct":
                cs += 1
            else:
                cs_done = True
        if not ws_done:
            if t == "answer_wrong":
                ws += 1
            else:
                ws_done = True
        if not ss_done:
            if t in ("skip", "boredom_flag"):
                ss += 1
            elif t in ("answer_correct", "answer_wrong", "followup"):
                ss_done = True
        if not fs_done:
            if t == "followup":
                fs += 1
            elif t in ("answer_correct", "answer_wrong", "skip", "boredom_flag"):
                fs_done = True
    return {"correct_streak": cs, "wrong_streak": ws, "skip_streak": ss,
            "followup_streak": fs, "boredom_recent": boredom_recent}


class Engine:
    """状态机:apply_event 是地图唯一写入口。"""

    def __init__(self, store, now=None):
        self.store = store
        self._now = now or (lambda: int(time.time()))

    # ---- 会话 / 树 ----
    def create_session(self, domain: str) -> str:
        ts = self._now()
        sid = f"s_{uuid.uuid4().hex[:10]}"
        self.store.create_session(sid, domain, ts)
        return sid

    def subtree(self, root_id: str) -> set:
        """root 本身 + 全部后代节点 id。"""
        ids = {root_id}
        frontier = [root_id]
        while frontier:
            cur = frontier.pop()
            n = self.store.node(cur)
            if n:
                for ch in n.children:
                    if ch not in ids:
                        ids.add(ch)
                        frontier.append(ch)
        return ids

    def exit_dive(self, session_id: str) -> bool:
        sess = self.store.session(session_id)
        if sess and sess.dive_node:
            sess.dive_node = None
            self.store.update_session(sess)
            return True
        return False

    # ---- 工具 ----
    def _resolve_node(self, ev: Event):
        nid = ev.node_id
        if not nid and ev.question_id:
            q = self.store.question(ev.question_id)
            nid = q.node_id if q else None
        if not nid:
            raise ValueError(f"event cannot resolve node_id: type={ev.type} q={ev.question_id}")
        node = self.store.node(nid)
        if not node:
            raise ValueError(f"unknown node: {nid}")
        return node.id, node

    def _session_followup_run(self, session_id: str, node_id: str) -> int:
        n = 0
        for e in self.store.events(session_id=session_id, ascending=False):
            if e.type == "followup" and (e.node_id or "") == node_id:
                n += 1
            elif e.type in ("answer_correct", "answer_wrong", "skip", "boredom_flag"):
                break
        return n

    def _session_skip_run(self, session_id: str) -> int:
        n = 0
        for e in self.store.events(session_id=session_id, ascending=False):
            if e.type == "skip":
                n += 1
            else:
                break
        return n

    def _pending_changed_answer(self, question_id: str) -> bool:
        """同一题距上次答分事件之间是否发生过 changed_answer → 折半记账。"""
        if not question_id:
            return False
        for e in self.store.events(question_id=question_id, ascending=False):
            if e.type == "changed_answer":
                return True
            if e.type in ANSWER_FAMILY:
                return False
        return False

    # ---- 主入口:地图唯一写入口 ----
    def apply_event(self, ev: Event) -> dict:
        if ev.type not in ALL_EVENT_TYPES:
            # §11.2 LLM 隔离落地点:伪装成事件的改状态指令在这里被拒。
            raise ValueError(f"unknown event type: {ev.type!r}")
        node_id, node = self._resolve_node(ev)
        ev.node_id = node_id
        sess = self.store.session(ev.session_id)
        if sess is None:
            raise ValueError(f"unknown session: {ev.session_id}")
        resp = {"event_type": ev.type, "node_id": node_id,
                "dive_entered": False, "dive_exited": False}

        if ev.type in ("answer_correct", "answer_wrong", "retention_check"):
            resp.update(self._settle(ev, node))
        elif ev.type == "skip":
            resp.update(self._event_skip(ev, node, sess))
        elif ev.type == "boredom_flag":
            resp.update(self._event_boredom(ev, node, sess))
        elif ev.type == "followup":
            resp.update(self._event_followup(ev, node, sess))
        elif ev.type == "changed_answer":
            resp.update(self._event_changed(ev, node))
        elif ev.type == "pause":
            resp.update(self._event_pause(ev, node))
        elif ev.type == "self_report":
            resp.update(self._event_self_report(ev, node))
        return resp

    # ---- 各类型 ----
    def _settle(self, ev: Event, node) -> dict:
        """answer_correct / answer_wrong / retention_check → 掌握度更新(SPEC §4.1)。"""
        st = self.store.state(node.id)
        before = st.mastery
        changed = (ev.payload.get("changed_from") is not None
                   or self._pending_changed_answer(ev.question_id))
        weight = CHANGED_WEIGHT if changed else 1.0
        obs = 1.0 if ev.type in ("answer_correct", "retention_check") else 0.0
        exp = expected(before, ev.difficulty or 0)
        k = k_factor(st.evidence_count)
        after = clip(before + k * weight * (obs - exp), 0, 100)

        # §11.8:事件里保留结算快照,供 §14 诊断离线计算
        ev.payload.setdefault("mastery_after", round(after, 2))
        ev.payload.setdefault("mastery_before", round(before, 2))
        self.store.append_event(ev)

        # draft 题首次判分通过 → approved(SPEC §6.3.3)
        approved = False
        if ev.question_id:
            q = self.store.question(ev.question_id)
            if q and q.qc_status == "draft" and ev.type == "answer_correct":
                self.store.approve_question(q.id)
                approved = True

        streaks = recompute_streaks(self.store, node.id)
        for name, val in streaks.items():
            setattr(st, name, val)
        st.mastery = after
        st.evidence_count += 1
        st.last_seen = ev.ts
        if ev.type == "answer_correct" and st.correct_streak == 3:
            st.difficulty_bias += BIAS_ON_STREAK3
        if ev.type == "answer_wrong" and st.wrong_streak == 2:
            st.difficulty_bias += BIAS_ON_WRONG2
        self.store.save_state(st)

        msg = nudges.settle(node.name, obs, before, after, ev.difficulty,
                            changed, st.difficulty_bias)
        return {"mastery_before": round(before, 2), "mastery_after": round(after, 2),
                "streaks": streaks, "difficulty_bias": st.difficulty_bias,
                "nudge": msg, "question_approved": approved}

    def _event_skip(self, ev: Event, node, sess) -> dict:
        reason = ev.payload.get("reason")
        if reason is not None and reason not in ("too_easy", "too_hard", "boring"):
            raise ValueError(f"bad skip reason: {reason!r}")
        self.store.append_event(ev)
        st = self.store.state(node.id)
        for name, val in recompute_streaks(self.store, node.id).items():
            setattr(st, name, val)
        if reason == "too_easy":
            st.difficulty_bias += BIAS_ON_TOO_EASY
        elif reason == "boring":
            st.difficulty_bias += BIAS_ON_BORING
        st.last_seen = ev.ts
        self.store.save_state(st)

        dive_exited = self._maybe_exit_dive(sess, node.id, ev.type)
        msg = nudges.skip(node.name, reason)
        return {"difficulty_bias": st.difficulty_bias, "nudge": msg,
                "dive_exited": dive_exited}

    def _event_boredom(self, ev: Event, node, sess) -> dict:
        self.store.append_event(ev)
        st = self.store.state(node.id)
        for name, val in recompute_streaks(self.store, node.id).items():
            setattr(st, name, val)
        st.difficulty_bias += BIAS_ON_BORING
        st.last_seen = ev.ts
        self.store.save_state(st)
        dive_exited = self._maybe_exit_dive(sess, node.id, ev.type)
        msg = nudges.bored(node.name)
        return {"difficulty_bias": st.difficulty_bias, "nudge": msg,
                "dive_exited": dive_exited}

    def _event_changed(self, ev: Event, node) -> dict:
        self.store.append_event(ev)
        st = self.store.state(node.id)
        st.last_seen = ev.ts  # recompute_streaks 会忽略 changed_answer,不影响任何 run
        return {"nudge": nudges.changed(node.name)}
        return {"nudge": f"已记录改答案:「{node.name}」;该题结算时打 {CHANGED_WEIGHT} 折"}

    def _event_pause(self, ev: Event, node) -> dict:
        self.store.append_event(ev)
        st = self.store.state(node.id)
        st.last_seen = ev.ts  # 停留是思考不是错误(SPEC §9)
        self.store.save_state(st)
        return {"nudge": nudges.pause(node.name, ev.payload.get("dwell_ms"))}

    def _event_followup(self, ev: Event, node, sess) -> dict:
        self.store.append_event(ev)
        st = self.store.state(node.id)
        for name, val in recompute_streaks(self.store, node.id).items():
            setattr(st, name, val)
        st.last_seen = ev.ts
        self.store.save_state(st)

        entered = False
        sess = self.store.session(sess.id)  # re-read:防止旧 dive 状态
        if sess.dive_node is None:
            run = self._session_followup_run(sess.id, node.id)
            if run >= DIVE_ENTER_N:
                sess.dive_node = node.id
                self.store.update_session(sess)
                entered = True
        return {"nudge": nudges.followup(node.name, st.followup_streak, entered),
                "dive_entered": entered}

    def _event_self_report(self, ev: Event, node) -> dict:
        st = self.store.state(node.id)
        before = st.mastery
        rating = float(ev.payload.get("self_rating", before))
        self.store.append_event(ev)
        moved = None
        if st.evidence_count < SELF_REPORT_MAX_EVIDENCE:
            moved = clip(before + (rating - before) * SELF_REPORT_WEIGHT, 0, 100)
            st.mastery = moved
        st.last_seen = ev.ts
        self.store.save_state(st)
        msg = nudges.self_report(node.name, rating, before, moved)
        return {"mastery_before": round(before, 2),
                "mastery_after": round(moved, 2) if moved is not None else round(before, 2),
                "nudge": msg}

    # ---- 深挖模式(SPEC §5.3) ----
    def _maybe_exit_dive(self, sess, node_id: str, ev_type: str) -> bool:
        sess = self.store.session(sess.id)
        if not sess or not sess.dive_node:
            return False
        exited = False
        if ev_type == "boredom_flag" and node_id in self.subtree(sess.dive_node):
            exited = True
        elif ev_type == "skip" and self._session_skip_run(sess.id) >= DIVE_EXIT_SKIP_RUN:
            exited = True
        if exited:
            sess.dive_node = None
            self.store.update_session(sess)
        return exited
