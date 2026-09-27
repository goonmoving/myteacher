"""重放(SPEC §11.1):events 全量顺序回放 → 重建一致地图。

重放假设施:同样 nodes/questions 配置 + 同样 session 创建 + 同样事件序列
→ states 与 sessions(dive)必须完全一致。事件里的 mastery_after 等快照
字段仅作记录,不参与计算——状态永远由配方从事件推导。
"""
from myteacher.events import Engine
from myteacher.model import Event


def replay(store, events, sessions=(), now=None):
    """在(已配好 nodes/questions 的)新 store 上回放事件流。"""
    eng = Engine(store, now=now)
    for sess in sessions:
        store.create_session(sess.id, sess.domain, sess.created_ts)
    for ev in events:
        eng.apply_event(ev)
    return eng


def snapshot(store) -> dict:
    """全量重放可比快照:states + sessions(含 dive 指针)。"""
    states = {nid: st.to_dict() for nid, st in store.states().items()}
    sessions = {}
    for row in store.conn.execute("SELECT * FROM sessions").fetchall():
        sessions[row["id"]] = {"domain": row["domain"], "dive_node": row["dive_node"]}
    return {"states": states, "sessions": sessions}


def event_log(store) -> list:
    """导出 events.log 全量(逐条 dict)——重放的输入/备份格式。"""
    return [{"id": e.id, "ts": e.ts, "session_id": e.session_id, "type": e.type,
             "question_id": e.question_id, "node_id": e.node_id,
             "difficulty": e.difficulty, "payload": e.payload}
            for e in store.all_events()]


def load_log(store, log, sessions=(), now=None):
    evs = [Event(id=it.get("id"), ts=it["ts"], session_id=it["session_id"],
                 type=it["type"], question_id=it.get("question_id"),
                 node_id=it.get("node_id"), difficulty=it.get("difficulty"),
                 payload=it.get("payload") or {})
           for it in log]
    return replay(store, evs, sessions=sessions, now=now)
