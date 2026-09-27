"""M3 聊天壳(交互全流程;业务全部走 api.Api——M2 已经成型)。

用法:python3 -m myteacher.chat [--db myteacher.db] [--session s_...] [music]
可脚本驱动(测试里直接调 Chat.step),print/ask 均可注入。
"""
import argparse
import os

from myteacher.api import Api
from myteacher.config import bootstrap
from myteacher.llm import build_llm
from myteacher.mapview import render_map
from myteacher.store import Store

HELP = ("答题直接输 A-D(或简答文本);命令:map / state / why / calibrate <分> / "
        "skip too_easy|too_hard|boring / bored / followup <文本> / exit")


class Chat:
    """一个会话的壳。pending = 最近一次 /v1/next(或深挖挑战)的 payload。"""

    def __init__(self, api: Api, domain: str, session_id=None,
                 out=print, ask=input):
        self.api = api
        self.domain = domain
        self.session_id = session_id
        self.out = out
        self.ask = ask
        self.pending = None
        self.last_node = None
        self.last_wrong = None      # 最后答错的 question payload
        self.last_result = None
        self.last_raw_answer = None

    # ---- 生命周期 ----
    def start(self):
        if not self.session_id:
            code, r = self.api.handle("POST", "/v1/sessions", {},
                                      {"domain": self.domain})
            if code != 200:
                self.out(str(r)); return ""
            self.session_id = r["session_id"]
        self.fetch_next()
        self.out(f"== myteacher 会话 {self.session_id}({self.domain})==")
        self.out(HELP)
        return self.show_question()

    # ---- 展示 ----
    def fetch_next(self) -> dict:
        code, r = self.api.handle("GET", "/v1/next", {"session": self.session_id})
        if code != 200:
            raise RuntimeError(str(r))
        self.pending = r
        return r

    def show_question(self) -> str:
        if self.pending is None:
            self.fetch_next()
        q = self.pending["question"]
        tag = "【深挖】" if self.pending.get("dive_active") else ""
        lines = [f"[{self.pending['action_type']}] {tag}{self.pending['reason']}"]
        lines.append(q["body"].get("prompt", ""))
        for i, opt in enumerate(q["body"].get("options", [])):
            lines.append(f"  {'ABCD'[i]}. {opt}")
        text = "\n".join(lines)
        self.out(text)
        return text

    def show_nudge(self, r):
        if r.get("nudge"):
            self.out(r["nudge"])
        if r.get("dive_entered"):
            self.out("(已进入深挖模式)")
        if r.get("dive_exited"):
            self.out("(深挖模式退出)")

    # ---- 一步 ----
    def step(self, line: str) -> bool:
        """处理一行输入;返回 False = 退出会话。"""
        line = (line or "").strip()
        if line in ("exit", "quit", "q"):
            self.out("下次见——地图已记下你的每一步。")
            return False
        if line in ("", "?", "help"):
            self.out(HELP)
            return True
        if line == "map":
            code, m = self.api.handle("GET", "/v1/map", {"domain": self.domain})
            if code != 200:
                self.out(str(m)); return True
            self.out(render_map(m))
            return True
        if line == "skip":
            line = "skip boring"
        if line.startswith("skip "):
            reason = line[len("skip "):].strip()
            code, r = self.api.handle("POST", "/v1/skips", {},
                                      self._with_pending(reason=reason))
            if code != 200:
                self.out(str(r)); return True
            self.show_nudge(r)
            self.pending = None
            self.show_question()
            return True
        if line == "bored":
            code, r = self.api.handle("POST", "/v1/boredoms", {},
                                      self._with_pending())
            if code != 200:
                self.out(str(r)); return True
            self.show_nudge(r)
            self.pending = r.get("next")
            self.show_question()
            return True
        if line.startswith("followup"):
            code, r = self.api.handle("POST", "/v1/followups", {},
                                      self._with_pending(text=line[8:].strip()))
            if code != 200:
                self.out(str(r)); return True
            self.show_nudge(r)
            if r.get("reply"):
                self.out(r["reply"])
            if r.get("next"):
                self.pending = r["next"]
                self.show_question()
            return True
        if line == "why":
            return self._why()
        if line.startswith("calibrate"):
            _, _, score = line.partition(" ")
            node = self.last_node or (self.pending or {}).get("node_id")
            try:
                rating = float(score)
            except ValueError:
                self.out("用法:calibrate 60"); return True
            code, r = self.api.handle("POST", "/v1/map/self-calibrate", {},
                                      {"node_id": node, "self_rating": rating})
            if code != 200:
                self.out(str(r)); return True
            self.out(r["nudge"])
            return True
        if line == "state":
            return self._state()
        return self._answer(line)

    def _with_pending(self, **extra) -> dict:
        if self.pending is None:
            self.fetch_next()
        d = {"question_id": self.pending["question"]["id"],
             "session_id": self.session_id}
        d.update(extra)
        return d

    def _answer(self, line: str) -> bool:
        if self.pending is None:
            self.fetch_next()
        q = self.pending["question"]
        if q["format"] != "mcq":
            answer = line
        else:
            idx = "ABCD".find(line.upper())
            if idx < 0:
                self.out(f"(无法解析输入:{line!r};help 看用法)")
                return True
            answer = idx
        code, r = self.api.handle("POST", "/v1/answers", {},
                                  {"question_id": q["id"],
                                   "session_id": self.session_id,
                                   "answer": answer})
        if code == 409:            # §6.1 低置信 → 用户自验
            v = (self.ask("判分低置信,请你自判:答对了吗?(y/n)> ") or "").lower()
            code, r = self.api.handle("POST", "/v1/answers", {},
                                      {"question_id": q["id"],
                                       "session_id": self.session_id,
                                       "answer": answer, "confirm": v == "y"})
        if code != 200:
            self.out(str(r)); return True
        self.last_node = q["node_id"]
        self.last_result = r
        self.last_raw_answer = answer
        if not r.get("judge", {}).get("correct", True):
            self.last_wrong = q
        self.show_nudge(r)
        if r.get("explanation"):
            self.out(f"讲评:{r['explanation']}")
        st = r["node_snapshot"]
        self.out(f"(mastery={st['mastery']} ev={st['evidence_count']} "
                 f"bias={st['difficulty_bias']})")
        self.pending = None
        self.show_question()
        return True

    def _why(self) -> bool:
        if not self.last_wrong:
            self.out("没有存档的错题(答错后我才有讲评)。")
            return True
        q = self.api.engine.store.question(self.last_wrong["id"])
        st = self.api.engine.store.state(q.node_id)
        txt = None
        if self.api.llm is not None and self.api.llm.name != "stub":
            try:
                txt = self.api.llm.explain(q, self.last_raw_answer, st)
            except Exception:
                txt = None
        self.out(txt or self.last_result.get("explanation") or "(暂无讲评来源)")
        return True

    def _state(self) -> bool:
        r = self.last_result
        if not r:
            self.out("还没有答题记录;先答一题。")
            return True
        self.out(str(r["node_snapshot"]))
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("MYTEACHER_DB", "myteacher.db"))
    ap.add_argument("--session", default=None,
                    help="复用既有会话(继续昨天的地图)")
    ap.add_argument("domain", nargs="?", default="music")
    args = ap.parse_args()
    store = Store(args.db)
    bootstrap(store)
    api = Api(store, llm=build_llm())
    chat = Chat(api, args.domain, session_id=args.session, out=print)
    chat.start()
    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not chat.step(line):
            break


if __name__ == "__main__":
    main()
