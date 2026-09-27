"""领域配置与种子题加载(SPEC §2.4 / §6.3)。

- domains/*.json:技能树配置(树是数据,不写死在代码里)
- seed/questions_seed.json:最小可用种子题库(M1 stub 语义,见 README)
"""
import json
import os
from pathlib import Path

from myteacher.model import Question, SkillNode

REPO_ROOT = Path(__file__).resolve().parent.parent
DOMAIN_DIR = REPO_ROOT / "domains"
SEED_FILE = REPO_ROOT / "seed" / "questions_seed.json"


def load_domain(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_domains(dirpath=None) -> list:
    dirpath = Path(dirpath or DOMAIN_DIR)
    return [load_domain(p) for p in sorted(dirpath.glob("*.json"))]


def bootstrap(store, repo_root=None):
    """幂等:把 domains/*.json 与 seed 题灌进 store(已存在的不覆盖)。"""
    root = Path(repo_root) if repo_root else REPO_ROOT
    ddir = root / "domains"
    for cfg in load_domains(ddir):
        for nd in cfg["nodes"]:
            store.upsert_node(SkillNode(id=nd["id"], domain=cfg["domain"],
                                        name=nd["name"], parent=nd.get("parent"),
                                        children=nd.get("children", []),
                                        prereqs=nd.get("prereqs", [])))
    seed = root / "seed" / "questions_seed.json"
    if seed.exists():
        with open(seed, encoding="utf-8") as f:
            data = json.load(f)
        for qd in data["questions"]:
            store.add_question(Question(
                id=qd["id"], node_id=qd["node_id"], difficulty=qd["difficulty"],
                format=qd.get("format", "mcq"), kind=qd.get("kind", "concept"),
                body=qd.get("body", {}), answer_key=qd.get("answer_key", {}),
                source=qd.get("source", "seed"),
                qc_status=qd.get("qc_status", "approved")))
    store.conn.commit()


def domain_map(store, domain: str) -> dict:
    """认知地图可视化数据(SPEC §7 GET /v1/map)。"""
    nodes = store.nodes(domain)
    out = []
    for n in nodes:
        st = store.state(n.id)
        out.append({"id": n.id, "name": n.name, "parent": n.parent,
                    "state": st.to_dict()})
    dome = {"domain": domain, "nodes": out}
    return dome
