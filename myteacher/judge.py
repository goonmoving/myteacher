"""判分通道(SPEC §6)。

事件流只存已判分结果;这里负责把"作答"变成 answer_correct/wrong。
- mcq:确定性判分,confidence 1.0
- free/explain:M1 无 LLM → 低置信,needs_user=True(用户自验后确认)
M1 中真实 LLM 判分是 M2 的事;本模块只定通道形状。
"""
from typing import Optional


def judge_answer(question, user_answer, llm=None) -> dict:
    fmt = question.format
    if fmt == "mcq":
        try:
            idx = int(user_answer)
        except (TypeError, ValueError):
            return {"correct": False, "confidence": 1.0, "needs_user": False,
                    "error": "answer must be option index"}
        correct = idx == question.answer_key.get("answer")
        return {"correct": correct, "confidence": 1.0, "needs_user": False}
    if llm is not None:
        v = llm.judge(question, user_answer)
        # 白名单过滤:判分器不可借机夹带改状态字段
        return {"correct": bool(v.get("correct")),
                "confidence": float(v.get("confidence", 0.0)),
                "needs_user": bool(v.get("needs_user", False))}
    return {"correct": False, "confidence": 0.0, "needs_user": True}
