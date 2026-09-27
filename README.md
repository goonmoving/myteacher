# myteacher — 学习状态模型

> 文档地图:[PRINCIPLES.md](PRINCIPLES.md) 原理与过程 · [**USAGE.md**](USAGE.md) 通俗使用手册(新上手从这里开始) · [SPEC.md](SPEC.md) 设计规格

## M1 已实现(SPEC §12)

- `myteacher/model.py` — SkillNode / SkillState / Question / Event / Session(§2)
- `myteacher/events.py` — 事件管线 = 地图唯一写入口:Elo 风格掌握度更新(§4)、
  streak 重算(最近 N=5)、难度 bias 规则(§4.2)、深挖模式进出(§5.3)
- `myteacher/policy.py` — 下一题策略:评分函数 + Next Best Action
  (learn/review/variant/transfer/rescue)+ 反无聊偏航(§11.3)
- `myteacher/llm.py` — StubLLM(确定性桩;M2 换真实适配层)
- `myteacher/judge.py` — 判分通道:mcq 确定性;free 低置信转用户自验(§6.1)
- `myteacher/store.py` / `myteacher/replay.py` — SQLite + 重放(§11.1)
- `myteacher/cli.py` — 冒烟 CLI
- `tests/` — §11.1–5、7、8 验收 fixtures(unittest,零依赖)

## 快速开始

```bash
# 跑验收测试(SPEC §11)
python3 -m unittest discover -s tests -v

# 冒烟一轮试试(冒烟库在 /tmp)
python3 -m myteacher.cli --db /tmp/mt.db music
# 目录内直接测某一领域:perspective / music / literature
```

CLI 命令:`A/B/C/D` 答题;`skip too_easy|too_hard|boring`;`bored`(喊无聊);
`followup <文本>`(连续两次触发深挖模式);`exit`。

## M1 约定与桩

- 种子题面是 stub 语义(qc=approved),只为状态机验收;真实题面 M2 由 LLM 生成 + QC 入轨(§6.3)。
- spec §3.1 的 `retention_check` 在实现中表现为 `answer_*` 事件 + `payload.retention=true`(v1 未启用调度);`pause` 仅更新 last_seen。
- `self_confidence` / `mastery_after` 已随 answer 事件落库,但 v1 只存不算(§14 报表在 v2)。
- dive 的挑战题由 LLM 按 (node, mastery+20, explain) 现场生成。

## M2 已实现(SPEC §7 API + §6 LLM)

- `myteacher/api.py` — 框架无关路由层 `Api.handle(method,path,query,body)`;§7 全部
  13 个端点;判分低置信 → 409 请用户自验(§6.1);`/v1/replay` 与 live 状态对拍。
- `myteacher/server.py` — 零依赖 stdlib HTTP 服务器默认档:
  `python3 -m myteacher.server --db myteacher.db --port 8787`
  (PEP 668 受管环境装不了 FastAPI 时的兜底;要 FastAPI 自己装后可再挂 Api 层)。
- `myteacher/llm.py` — `OpenAICompatLLM`(urllib 实现,超时+1 次重试+严格 JSON)
  + `sanitize_question` 白名单清洗(node_id 强制归属本节点、恶意字段剥离、
  mcq 选项/答案校验、difficulty clamp);环境变量:`MYTEACHER_LLM_BASE_URL/
  API_KEY/MODEL`,缺省 → StubLLM(无网可跑)。
- QC 入轨:auto-approve 挂在事件上(draft 首次判分通过 → approved,§6.3.3);
  `/v1/questions/drafts` + `/v1/questions/{id}/approve` 手动通道。

测试:`tests/test_api.py`(全流程 / 低置信 / 验证 / 404)、
`tests/test_llm_adapter.py`(白名单清洗 / 重试 / JSON 解析)。

## M3 已实现(SPEC §12)

- `myteacher/chat.py` — 聊天壳:业务全走 `api.Api`(不绕过状态机);
  命令 `map / state / why / calibrate <分> / skip / bored / followup <文本>`;
  低置信判分(409)当场追问用户自验;out/ask 可注入,纯脚本可测。
- `myteacher/nudges.py` — nudge 文案系统化:全部模板集中一处,
  `tests/test_nudges.py` 与 events 常量对拍防漂移。
- `myteacher/mapview.py` — 终端认知地图渲染:10 格掌握度条 +
  [估](证据<3)/ ▼低洼(<40)/ ▲精通(>85),输出确定性。
- `web/map.html` — 浏览器可视化:`?api=http://127.0.0.1:8787` 连
  `/v1/map` 实时渲染;连不上自动降级为演示数据(SPEC §2.4 示例)。

即时体验:

```bash
python3 -m myteacher.server --port 8787            # 终端 1
python3 -m myteacher.chat --db myteacher.db music  # 终端 2(聊天壳)
# 浏览器打开 web/map.html 看地图实时变化
```

## M4 已实现(SPEC §12 / §14)

- `myteacher/diagnostics.py` — §14 三类报表(纯只读,不回流状态):
  迁移差距(join `question.kind`,节点级 gap>20pp 触发"缺的不是理解,是迁移")、
  保持曲线(同节点相邻判分间隔 → <1d/1-7d/7-30d/30d+ 分桶,早强晚弱触发
  "长期保持不足")、自信校准(`self_confidence` vs 实际正确率,±15pp 触发
  高估/低估结论)。
- `GET /v1/diagnostics?domain=…`(API §7 新行)。
- `myteacher/tune.py` — §5.1 权重调参 harness:合成用户闭环(真实能力和难度
  决定答题概率,过度简单触发 boring skip),网格搜索 Weights;支持
  `--log events.jsonl` 真实日志模式(重放 → 覆盖率 + 诊断)。种子题库扩到
  56 题:每节点 concept×2 / variant×1 / transfer×1。

```bash
python3 -m myteacher.tune --domain music --rounds 240   # 网格调参
curl "localhost:8787/v1/diagnostics?domain=music"       # 诊断报表
```

## 后续(v2 候选)

- Transfer 策略接线(把 transfer_gap 反馈进 §5.1 评分)、遗忘漂移、
  树自动细分、多领域并行会话、mastery 不确定度显示。
