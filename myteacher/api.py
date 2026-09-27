"""框架无关 API 路由层(SPEC §7)。

Api.handle(method, path, query, body) -> (status:int, payload:dict)
- server.py(stdlib,零依赖)/ 可选的 FastAPI 壳都只挂这一层,业务只有一份。
- 约定:ValueError→400;未找到资源→404;策略无法出题→503;其余 200。
"""
import time

from myteacher.config import REPO_ROOT, bootstrap, domain_map, load_domains
from myteacher.events import Engine
from myteacher.judge import judge_answer
from myteacher.model import Event, Session
from myteacher.policy import choose_next
from myteacher.replay import load_log, snapshot
from myteacher.store import Store


class Api:
    def __init__(self, store, llm=None, now=None):
        self.store = store
        self.llm = llm
        self.engine = Engine(store, now=now)
        self.now = now or (lambda: int(time.time()))

    # ---- 小工具 ----
    def _s(self, body, key, required=True, default=None):
        v = (body or {}).get(key, default)
        if required and v is None:
            raise ValueError(f"missing field: {key}")
        return v

    def _question_or_404(self, qid):
        q = self.store.question(qid or "") if qid else None
        if q is None:
            raise KeyError(f"question not found: {qid}")
        return q

    @staticmethod
    def _q_json(q) -> dict:
        return {"id": q.id, "node_id": q.node_id, "difficulty": q.difficulty,
                "format": q.format, "kind": q.kind, "body": q.body}

    def _next_payload(self, choice) -> dict:
        return {"question": self._q_json(choice.question),
                "node_id": choice.node_id,
                "target_difficulty": choice.target_difficulty,
                "action_type": choice.action_type, "reason": choice.reason,
                "dive_active": choice.dive_active}

    # ---- 主入口 ----
    def handle(self, method: str, path: str, query: dict = None,
               body: dict = None):
        query = query or {}
        try:
            if method == "GET" and path == "/v1/health":
                return 200, {"ok": True, "version": "0.1.0"}
            if method == "POST" and path == "/v1/sessions":
                return self._sessions(body)
            if method == "GET" and path == "/v1/next":
                return self._next(query)
            if method == "POST" and path == "/v1/answers":
                return self._answers(body)
            if method == "POST" and path == "/v1/skips":
                return self._skips(body)
            if method == "POST" and path == "/v1/followups":
                return self._followups(body)
            if method == "POST" and path == "/v1/boredoms":
                return self._boredoms(body)
            if method == "GET" and path == "/v1/map":
                return self._map(query)
            if method == "POST" and path == "/v1/map/self-calibrate":
                return self._self_calibrate(body)
            if method == "POST" and path == "/v1/events":
                return self._events_inject(body)
            if method == "GET" and path == "/v1/events":
                return self._events_export(query)
            if method == "POST" and path == "/v1/questions/drafts":
                return self._draft(body)
            if method == "POST" and path.startswith("/v1/questions/") \
                    and path.endswith("/approve"):
                return self._approve(path)
            if method == "GET" and path == "/v1/replay":
                return self._replay(query)
            if method == "GET" and path == "/v1/diagnostics":
                return self._diagnostics(query)
            return 404, {"error": f"no route: {method} {path}"}
        except KeyError as ex:
            return 404, {"error": str(ex.args[0] if ex.args else ex)}
        except ValueError as ex:
            return 400, {"error": str(ex)}
        except RuntimeError as ex:
            return 503, {"error": str(ex)}

    # ---- 端点 ----
    def _sessions(self, body):
        domain = self._s(body, "domain")
        known = {c["domain"] for c in load_domains()}
        if domain not in known:
            raise ValueError(f"unknown domain: {domain}")
        sid = self.engine.create_session(domain)
        return 200, {"session_id": sid, "domain": domain}

    def _next(self, query):
        sid = query.get("session") or ""
        choice = choose_next(self.store, sid, self.now(), llm=self.llm)
        return 200, self._next_payload(choice)

    def _answers(self, body):
        q = self._question_or_404(self._s(body, "question_id"))
        sid = self._s(body, "session_id")
        judge = judge_answer(q, self._s(body, "answer"), llm=self.llm)
        if judge.get("needs_user") and body.get("confirm") is None:
            # §6.1 低置信 → 请用户自验后带 confirm 重发
            return 409, {"error": "judge needs user confirmation", "judge": judge,
                         "nudge": "判分低置信:请自己确认对/错后,带 confirm 重发。"}
        if body.get("confirm") is not None:
            judge = {"correct": bool(body["confirm"]), "confidence": 0.0,
                     "needs_user": False}
        # 改答案:先落 changed_answer 事件(结算折半),再落判分事件
        changed_from = body.get("changed_from")
        if changed_from is not None:
            self.engine.apply_event(Event(
                ts=self.now(), session_id=sid, type="changed_answer",
                question_id=q.id,
                payload={"from": changed_from, "to": body.get("answer")}))
        payload = {k: body[k] for k in ("dwell_ms", "self_confidence",
                                        "retention") if k in body}
        ev_type = "answer_correct" if judge["correct"] else "answer_wrong"
        r = self.engine.apply_event(Event(
            ts=self.now(), session_id=sid, type=ev_type, question_id=q.id,
            difficulty=q.difficulty, payload=payload))
        st = self.store.state(q.node_id)
        explanation = None
        if not judge["correct"] and self.llm is not None:
            try:
                explanation = self.llm.explain(q, body.get("answer"), st)
            except Exception:      # 解释失败不影响记账
                explanation = None
        return 200, {"judge": judge, "explanation": explanation,
                     "node_snapshot": st.to_dict(), "nudge": r["nudge"],
                     "mastery": {"before": r["mastery_before"],
                                 "after": r["mastery_after"]}}

    def _skips(self, body):
        q = self._question_or_404(self._s(body, "question_id"))
        r = self.engine.apply_event(Event(
            ts=self.now(), session_id=self._s(body, "session_id"), type="skip",
            question_id=q.id, difficulty=q.difficulty,
            payload={"reason": body.get("reason")}))
        return 200, {"nudge": r["nudge"], "dive_exited": r["dive_exited"]}

    def _followups(self, body):
        sid = self._s(body, "session_id")
        q = self._question_or_404(self._s(body, "question_id"))
        text = body.get("text") or ""
        r = self.engine.apply_event(Event(
            ts=self.now(), session_id=sid, type="followup",
            question_id=q.id, difficulty=q.difficulty, payload={"text": text}))
        st = self.store.state(q.node_id)
        target = max(15, min(95, round(st.mastery + 20)))
        try:
            reply = (self.llm.followup_reply(q, text, target)
                     if self.llm.name != "stub" else
                     f"已记录追问;围绕「{q.node_id}」接下来出难度 {target} 的挑战。")
        except Exception:
            reply = None
        out = {"nudge": r["nudge"], "dive_entered": r["dive_entered"], "reply": reply}
        if r["dive_entered"]:
            nxt = choose_next(self.store, sid, self.now(), llm=self.llm)
            out["next"] = self._next_payload(nxt)
        return 200, out

    def _boredoms(self, body):
        sid = self._s(body, "session_id")
        q = self._question_or_404(self._s(body, "question_id"))
        r = self.engine.apply_event(Event(
            ts=self.now(), session_id=sid, type="boredom_flag",
            question_id=q.id, difficulty=q.difficulty,
            payload={"text": body.get("text") or ""}))
        nxt = choose_next(self.store, sid, self.now(), llm=self.llm)
        return 200, {"nudge": r["nudge"], "dive_exited": r["dive_exited"],
                     "next": self._next_payload(nxt)}

    def _map(self, query):
        domain = query.get("domain") or ""
        known = {c["domain"] for c in load_domains()}
        if domain not in known:
            raise ValueError(f"unknown domain: {domain}")
        return 200, domain_map(self.store, domain)

    def _self_calibrate(self, body):
        nid = self._s(body, "node_id")
        rating = float(self._s(body, "self_rating"))
        node = self.store.node(nid)
        if node is None:
            raise KeyError(f"node not found: {nid}")
        sid = body.get("session_id") or self.engine.create_session(node.domain)
        r = self.engine.apply_event(Event(
            ts=self.now(), session_id=sid, type="self_report", node_id=nid,
            payload={"self_rating": rating}))
        return 200, {"nudge": r["nudge"],
                     "node_snapshot": self.store.state(nid).to_dict()}

    def _events_inject(self, body):
        ev = Event(id=body.get("id"), ts=int(body.get("ts") or self.now()),
                   session_id=self._s(body, "session_id"),
                   type=self._s(body, "type"), question_id=body.get("question_id"),
                   node_id=body.get("node_id"), difficulty=body.get("difficulty"),
                   payload=body.get("payload") or {})
        return 200, self.engine.apply_event(ev)

    def _events_export(self, query):
        evs = self.store.events(session_id=query.get("session"),
                                node_id=query.get("node"))
        limit = int(query.get("limit") or 0)
        if limit:
            evs = evs[-limit:]
        return 200, {"events": [{"id": e.id, "ts": e.ts,
                                 "session_id": e.session_id, "type": e.type,
                                 "question_id": e.question_id,
                                 "node_id": e.node_id,
                                 "difficulty": e.difficulty,
                                 "payload": e.payload} for e in evs]}

    def _draft(self, body):
        if self.llm is None or getattr(self.llm, "name", "") == "stub":
            return 400, {"error": "set MYTEACHER_LLM_BASE_URL to draft via real LLM"}
        node = self.store.node(self._s(body, "node_id") or "")
        if node is None:
            raise KeyError(f"node not found: {body.get('node_id')}")
        target = int(self._s(body, "difficulty", required=False, default=50))
        fmt = self._s(body, "format", required=False, default="mcq")
        q = self.llm.generate_question(node, target, fmt)
        self.store.add_question(q)
        return 200, {"question": q.__dict__}

    def _approve(self, path):
        qid = path[len("/v1/questions/"):-len("/approve")]
        self._question_or_404(qid)
        self.store.approve_question(qid)

    def _diagnostics(self, query) -> tuple:
        domain = query.get("domain") or ""
        known = {c["domain"] for c in load_domains()}
        if domain not in known:
            raise ValueError(f"unknown domain: {domain}")
        from myteacher.diagnostics import report
        return 200, report(self.store, domain)

    def _replay(self, query):
        sid = query.get("session") or ""
        sess = self.store.session(sid)
        if sess is None:
            raise KeyError(f"session not found: {sid}")
        log = [{"id": e.id, "ts": e.ts, "session_id": e.session_id,
                "type": e.type, "question_id": e.question_id,
                "node_id": e.node_id, "difficulty": e.difficulty,
                "payload": e.payload}
               for e in self.store.events(session_id=sid)]
        rep = Store(":memory:")
        bootstrap(rep, REPO_ROOT)
        sobj = Session(id=sess.id, domain=sess.domain,
                       created_ts=(log[0]["ts"] - 1 if log else 0))
        load_log(rep, log, sessions=[sobj])
        rep_snap = snapshot(rep)
        live_only_states = snapshot(self.store)["states"]
        matched = (rep_snap["states"] == live_only_states
                   if len(self.store.events()) == len(log) else None)
        return 200, {"events": len(log), "snapshot_replay": rep_snap,
                     "matches_live": matched}
