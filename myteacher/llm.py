"""LLM 适配层(SPEC §6,可插拔)。

硬边界(§6.2):
- 产出只能是 Question(draft) / 判分结论 / 解释文本 —— 不能触碰 SkillState。
- Question 经 sanitize 白名单清洗,多余字段进不了地图。
- 判分 confidence < 0.8 → needs_user=True,由用户自验后确认(§6.1)。

M1:StubLLM(确定性,不联网)。
M2:OpenAICompatLLM(OpenAI 兼容 /chat/completions;urllib 实现,零依赖)。
环境变量:
  MYTEACHER_LLM_BASE_URL   如 https://api.openai.com/v1(省略则用 StubLLM)
  MYTEACHER_LLM_API_KEY
  MYTEACHER_LLM_MODEL      默认 gpt-4o-mini
"""
import json
import os
import time
import urllib.error
import urllib.request
import uuid

from myteacher.model import FORMATS, QUESTION_KINDS, Question

JUDGE_CONFIDENCE_GATE = 0.8


class LLMError(Exception):
    pass


def _clamp_diff(d, default) -> int:
    try:
        d = int(d)
    except (TypeError, ValueError):
        d = int(default)
    return max(15, min(95, d))


def sanitize_question(raw: dict, node, target_difficulty: int, fmt: str) -> Question:
    """白名单清洗(SPEC §6.3):LLM 的原始输出只有这些字段能进系统。"""
    if not isinstance(raw, dict):
        raise LLMError("question payload must be a JSON object")
    body = raw.get("body")
    if not isinstance(body, dict) or not isinstance(body.get("prompt"), str) \
            or not body["prompt"].strip():
        raise LLMError("body.prompt missing")
    fmt = raw.get("format") if raw.get("format") in FORMATS else fmt
    kind = raw.get("kind") if raw.get("kind") in QUESTION_KINDS else "concept"
    difficulty = _clamp_diff(raw.get("difficulty"), target_difficulty)
    answer_key = raw.get("answer_key") or {}
    if fmt == "mcq":
        options = body.get("options")
        if not isinstance(options, list) or len(options) < 2:
            raise LLMError("mcq needs body.options (>=2)")
        ans = answer_key.get("answer")
        if not isinstance(ans, int) or not (0 <= ans < len(options)):
            raise LLMError("answer_key.answer out of range")
    else:
        pts = answer_key.get("rubric_points")
        if not isinstance(pts, list) or not pts or not all(isinstance(p, str) for p in pts):
            raise LLMError(f"{fmt} needs answer_key.rubric_points (list of str)")
    qid = f"q_llm_{node.id.replace('.', '_')}_{difficulty}_{uuid.uuid4().hex[:8]}"
    return Question(id=qid, node_id=node.id, difficulty=difficulty,
                    format=fmt, kind=kind, body=body,
                    answer_key=answer_key, source="llm", qc_status="draft")


class StubLLM:
    """确定性桩:不联网、可重放,只为 M1 打通状态机。"""

    name = "stub"

    def generate_question(self, node, target_difficulty: int, fmt: str):
        if fmt == "mcq":
            body = {"prompt": f"[stub] 「{node.name}」概念检验(难度 {target_difficulty}):"
                              f"以下哪项最接近该技能的核心要点?",
                    "options": ["要点甲", "要点乙", "要点丙", "要点丁"]}
            answer_key = {"answer": 0}
        else:
            body = {"prompt": f"[stub] 「{node.name}」{fmt} 题(难度 {target_difficulty}):"
                              f"用你自己的话讲清楚一个要点。"}
            answer_key = {"rubric_points": ["要点甲"]}
        qid = f"q_stub_{node.id}_{target_difficulty}_{fmt}"
        return Question(id=qid, node_id=node.id, difficulty=int(target_difficulty),
                        format=fmt, kind="concept", body=body,
                        answer_key=answer_key, source="stub_llm", qc_status="draft")

    def judge(self, question, user_answer):
        """free/explain 的桩判分:低置信,交给用户自验(SPEC §6.1)。"""
        return {"correct": False, "confidence": 0.0, "needs_user": True}

    def explain(self, question, user_answer, state) -> str:
        return f"[stub] 「{question.node_id}」答错讲评:难度 {question.difficulty}," \
               f"mastery {state.mastery:.0f}%,下一步会降难度再试。"

    def followup_reply(self, question, text, target: int) -> str:
        return f"[stub] 围绕「{question.node_id}」深入({text or '追问'}):" \
               f"先接一道难度 {target} 的挑战。"


class OpenAICompatLLM:
    """OpenAI 兼容 chat/completions 适配(urllib,超时/重试/严格 JSON 解析)。"""

    name = "openai-compat"

    def __init__(self, base_url: str, api_key: str = "", model: str = "gpt-4o-mini",
                 timeout: int = 45):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    # ---- HTTP plumbing ----
    def _chat(self, messages, json_mode=True, temperature=0.7) -> str:
        payload = {"model": self.model, "messages": messages, "temperature": temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            f"{self.base}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        err = None
        for _ in range(2):  # 一次重试(SPEC §10 LLM 适配层可插拔,不背重试框架)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
            except Exception as ex:                      # noqa: BLE001(网络栈多样)
                err = ex
        raise LLMError(f"chat completions failed after retry: {err}")

    @staticmethod
    def _parse_json(text: str) -> dict:
        t = text.strip()
        if t.startswith("```"):
            t = t.strip("`") if t.count("```") == 2 else t
            if t.startswith("json"):
                t = t[4:]
        try:
            v = json.loads(t)
        except json.JSONDecodeError as ex:
            raise LLMError(f"LLM answer is not JSON: {ex}") from ex
        if not isinstance(v, dict):
            raise LLMError("LLM answer must be a JSON object")
        return v

    # ---- 能力(SPEC §6.1) ----
    def generate_question(self, node, target_difficulty: int, fmt: str):
        sys_p = ("你是题生成器。只输出 JSON,字段:"
                 '{"node_id", "difficulty", "format", "kind", '
                 '"body": {"prompt", "options"?}, "answer_key": {"answer"?} 或 {"rubric_points"}}。'
                 "mcq 须含 options(≥2)与 answer_key.answer(选项序号);"
                 "free/explain 须含 answer_key.rubric_points(要点列表)。")
        user_p = (f"node_id={node.id}(技能:{node.name});目标难度={target_difficulty}"
                  f"(0-100,与 mastery 同尺度);format={fmt}。"
                  f"出 1 道测「{node.name}」的题,难度贴近目标难度。")
        raw = self._parse_json(self._chat([
            {"role": "system", "content": sys_p},
            {"role": "user", "content": user_p}]))
        return sanitize_question(raw, node, target_difficulty, fmt)

    def judge(self, question, user_answer):
        rubric = json.dumps(question.answer_key, ensure_ascii=False)
        v = self._parse_json(self._chat([
            {"role": "system", "content": "你是判分器。只输出 JSON:"
                                          '{"correct": true/false, "confidence": 0.0-1.0}'
                                          "。宽松但一致;不确定度应在 confidence 里。"},
            {"role": "user", "content": f"题目:{json.dumps(question.body, ensure_ascii=False)}\n"
                                        f"评分依据:{rubric}\n用户作答:{user_answer!r}"}]))
        conf = max(0.0, min(1.0, float(v.get("confidence", 0.0))))
        return {"correct": bool(v.get("correct")), "confidence": conf,
                "needs_user": conf < JUDGE_CONFIDENCE_GATE}

    def explain(self, question, user_answer, state) -> str:
        txt = self._chat([
            {"role": "system", "content":
                "你是讲解者。用中文 2-3 句讲评:错在哪、该技能的判断要点是什么。"
                "只讲该技能节点,不延伸课程。"},
            {"role": "user", "content":
                f"技能:{state.node_id};mastery={state.mastery:.0f}%;"
                f"题目:{json.dumps(question.body, ensure_ascii=False)};"
                f"用户作答:{user_answer!r}"}],
            json_mode=False, temperature=0.4)
        return txt.strip()

    def followup_reply(self, question, text, target: int) -> str:
        txt = self._chat([
            {"role": "system", "content":
                "你是学习陪练。用户在题目上主动追问;用中文<=3句回应,"
                "不展开成课程,结尾一句把话题收回当前技能节点。"},
            {"role": "user", "content":
                f"技能:{question.node_id};题目:{json.dumps(question.body, ensure_ascii=False)};"
                f"用户追问:{text or '(无)'};接下来将出难度 {target} 的挑战题。"}],
            json_mode=False)
        return txt.strip()


def build_llm():
    """按环境变量装配;未配置 → StubLLM(无网可跑)。"""
    base = os.getenv("MYTEACHER_LLM_BASE_URL")
    if not base:
        return StubLLM()
    return OpenAICompatLLM(base, os.getenv("MYTEACHER_LLM_API_KEY", ""),
                           os.getenv("MYTEACHER_LLM_MODEL", "gpt-4o-mini"))
