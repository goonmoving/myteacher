"""SQLite 存储。append-only 的 events 是核心;其余为缓存/配置。

重放(SPEC §11.1):events 全量导出后,在同样 nodes/questions 配置的新库上
顺序回放,states/sessions 必须重建出一致结果。
"""
import json
import sqlite3
from typing import Optional

from myteacher.model import Event, Question, Session, SkillNode, SkillState


_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(
    id TEXT PRIMARY KEY, domain TEXT NOT NULL, name TEXT NOT NULL,
    parent TEXT, children_json TEXT NOT NULL DEFAULT '[]',
    prereqs_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS states(
    node_id TEXT PRIMARY KEY, mastery REAL NOT NULL, evidence_count INTEGER NOT NULL,
    last_seen INTEGER NOT NULL, correct_streak INTEGER NOT NULL,
    wrong_streak INTEGER NOT NULL, skip_streak INTEGER NOT NULL,
    followup_streak INTEGER NOT NULL, boredom_recent INTEGER NOT NULL,
    difficulty_bias INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS questions(
    id TEXT PRIMARY KEY, node_id TEXT NOT NULL, difficulty INTEGER NOT NULL,
    format TEXT NOT NULL, kind TEXT NOT NULL, body_json TEXT NOT NULL,
    answer_key_json TEXT NOT NULL, source TEXT NOT NULL, qc_status TEXT NOT NULL,
    served_at INTEGER NOT NULL DEFAULT 0, times INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, ts INTEGER NOT NULL,
    session_id TEXT NOT NULL, type TEXT NOT NULL, question_id TEXT,
    node_id TEXT, difficulty INTEGER, payload TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY, domain TEXT NOT NULL, dive_node TEXT,
    last_q_id TEXT, last_q_node TEXT, last_q_difficulty INTEGER,
    created_ts INTEGER NOT NULL DEFAULT 0);
"""


def _je(x) -> str:
    return json.dumps(x, ensure_ascii=False)


def _parse_je(x, default):
    return json.loads(x) if x else default


class Store:
    """单连接 SQLite;M1 单机单用户,不做并发。"""

    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    # ---- nodes -----------------------------------------------------------
    def upsert_node(self, n: SkillNode) -> None:
        self.conn.execute(
            "INSERT INTO nodes(id,domain,name,parent,children_json,prereqs_json)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET domain=excluded.domain,"
            " name=excluded.name, parent=excluded.parent,"
            " children_json=excluded.children_json, prereqs_json=excluded.prereqs_json",
            (n.id, n.domain, n.name, n.parent, _je(n.children), _je(n.prereqs)),
        )

    def node(self, node_id: str) -> Optional[SkillNode]:
        r = self.conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not r:
            return None
        return SkillNode(id=r["id"], domain=r["domain"], name=r["name"],
                         parent=r["parent"], children=_parse_je(r["children_json"], []),
                         prereqs=_parse_je(r["prereqs_json"], []))

    def nodes(self, domain: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE domain=? ORDER BY id", (domain,)).fetchall()
        return [SkillNode(id=r["id"], domain=r["domain"], name=r["name"], parent=r["parent"],
                          children=_parse_je(r["children_json"], []),
                          prereqs=_parse_je(r["prereqs_json"], [])) for r in rows]

    # ---- states ----------------------------------------------------------
    def state(self, node_id: str) -> SkillState:
        r = self.conn.execute("SELECT * FROM states WHERE node_id=?", (node_id,)).fetchone()
        if r:
            return SkillState(node_id=r["node_id"], mastery=r["mastery"],
                              evidence_count=r["evidence_count"], last_seen=r["last_seen"],
                              correct_streak=r["correct_streak"], wrong_streak=r["wrong_streak"],
                              skip_streak=r["skip_streak"], followup_streak=r["followup_streak"],
                              boredom_recent=r["boredom_recent"],
                              difficulty_bias=r["difficulty_bias"])
        return SkillState(node_id=node_id)

    def save_state(self, st: SkillState) -> None:
        self.conn.execute(
            "INSERT INTO states(node_id,mastery,evidence_count,last_seen,correct_streak,"
            "wrong_streak,skip_streak,followup_streak,boredom_recent,difficulty_bias)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(node_id) DO UPDATE SET mastery=excluded.mastery,"
            " evidence_count=excluded.evidence_count, last_seen=excluded.last_seen,"
            " correct_streak=excluded.correct_streak, wrong_streak=excluded.wrong_streak,"
            " skip_streak=excluded.skip_streak, followup_streak=excluded.followup_streak,"
            " boredom_recent=excluded.boredom_recent, difficulty_bias=excluded.difficulty_bias",
            (st.node_id, st.mastery, st.evidence_count, st.last_seen, st.correct_streak,
             st.wrong_streak, st.skip_streak, st.followup_streak, st.boredom_recent,
             st.difficulty_bias))

    def states(self) -> dict:
        rows = self.conn.execute("SELECT * FROM states").fetchall()
        return {r["node_id"]: SkillState(
            node_id=r["node_id"], mastery=r["mastery"], evidence_count=r["evidence_count"],
            last_seen=r["last_seen"], correct_streak=r["correct_streak"],
            wrong_streak=r["wrong_streak"], skip_streak=r["skip_streak"],
            followup_streak=r["followup_streak"], boredom_recent=r["boredom_recent"],
            difficulty_bias=r["difficulty_bias"]) for r in rows}

    # ---- events(append-only) ---------------------------------------------
    def append_event(self, ev: Event) -> int:
        cur = self.conn.execute(
            "INSERT INTO events(id,ts,session_id,type,question_id,node_id,difficulty,payload)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ev.id, ev.ts, ev.session_id, ev.type, ev.question_id, ev.node_id,
             ev.difficulty, _je(ev.payload)))
        return cur.lastrowid

    def events(self, session_id: Optional[str] = None, node_id: Optional[str] = None,
               question_id: Optional[str] = None,
               ascending: bool = True, limit: int = 0) -> list:
        sql, args = "SELECT seq,id,ts,session_id,type,question_id,node_id,difficulty,payload FROM events", []
        conds = []
        if session_id:
            conds.append("session_id=?"); args.append(session_id)
        if question_id:
            conds.append("question_id=?"); args.append(question_id)
        if node_id:
            conds.append("node_id=?"); args.append(node_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY seq" + ("" if ascending else " DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql, args).fetchall()
        return [Event(id=r["id"], ts=r["ts"], session_id=r["session_id"], type=r["type"],
                      question_id=r["question_id"], node_id=r["node_id"],
                      difficulty=r["difficulty"], payload=_parse_je(r["payload"], {}))
                for r in rows]

    def all_events(self) -> list:
        return self.events()

    # ---- questions ---------------------------------------------------------
    def add_question(self, q: Question, overwrite: bool = False) -> bool:
        exists = self.question(q.id) is not None
        if exists and not overwrite:
            return False
        self.conn.execute(
            "INSERT INTO questions(id,node_id,difficulty,format,kind,body_json,"
            "answer_key_json,source,qc_status,served_at,times)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET difficulty=excluded.difficulty,"
            " format=excluded.format, kind=excluded.kind, body_json=excluded.body_json,"
            " answer_key_json=excluded.answer_key_json, source=excluded.source,"
            " qc_status=excluded.qc_status",
            (q.id, q.node_id, q.difficulty, q.format, q.kind, _je(q.body),
             _je(q.answer_key), q.source, q.qc_status, q.served_at, q.times))
        return True

    def question(self, qid: str) -> Optional[Question]:
        r = self.conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        return self._row_q(r) if r else None

    def _row_q(self, r) -> Question:
        return Question(id=r["id"], node_id=r["node_id"], difficulty=r["difficulty"],
                        format=r["format"], kind=r["kind"],
                        body=_parse_je(r["body_json"], {}),
                        answer_key=_parse_je(r["answer_key_json"], {}),
                        source=r["source"], qc_status=r["qc_status"],
                        served_at=r["served_at"], times=r["times"])

    def candidate_questions(self, node_id: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM questions WHERE node_id=? ORDER BY difficulty,id", (node_id,)
        ).fetchall()
        return [self._row_q(r) for r in rows]

    def mark_served(self, qid: str, ts: int) -> None:
        self.conn.execute(
            "UPDATE questions SET served_at=?, times=times+1 WHERE id=?", (ts, qid))

    def approve_question(self, qid: str) -> None:
        self.conn.execute("UPDATE questions SET qc_status='approved' WHERE id=?", (qid,))

    # ---- sessions -----------------------------------------------------------
    def create_session(self, sid: str, domain: str, ts: int) -> Session:
        s = Session(id=sid, domain=domain, created_ts=ts)
        self.conn.execute(
            "INSERT INTO sessions(id,domain,dive_node,last_q_id,last_q_node,"
            "last_q_difficulty,created_ts) VALUES(?,?,NULL,NULL,NULL,NULL,?)",
            (sid, domain, ts))
        return s

    def session(self, sid: str) -> Optional[Session]:
        r = self.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not r:
            return None
        return Session(id=r["id"], domain=r["domain"], dive_node=r["dive_node"],
                       last_q_id=r["last_q_id"], last_q_node=r["last_q_node"],
                       last_q_difficulty=r["last_q_difficulty"], created_ts=r["created_ts"])

    def update_session(self, s: Session) -> None:
        self.conn.execute(
            "UPDATE sessions SET dive_node=?, last_q_id=?, last_q_node=?,"
            " last_q_difficulty=? WHERE id=?",
            (s.dive_node, s.last_q_id, s.last_q_node, s.last_q_difficulty, s.id))
