# myteacher — 学习状态模型 Spec(v1)

> 一句话定位:不做"AI 老师",不做"课程生成器"。系统维护一张**用户认知地图**(User Skill State):
> 所有交互(答题 / 停顿 / 改答案 / 跳过 / 追问)作为 Learning Events 写入地图,由**确定性策略**决定 Next Best Action
> (学新 / 复习 / 变式 / 迁移 / 回退);LLM 只负责把动作执行出来(出题 / 判分 / 解释 / 追问)——**LLM 永远不直接修改地图**。

---

## 1. 定位与原则

原问题描述(为什么不做课程):

- 固定课程节奏与真实认知状态不匹配
- 太简单 → 无聊;太重复 → 跳过
- 突然感兴趣的地方 → 深挖;真正难的地方 → 愿意停下来思考

对应的产品原则:

1. **地图是唯一事实源(Single Source of Truth)**。每个技能节点有一个掌握度分值,由事件流推导而来,可重放、可审计。
2. **决策是确定性的**。下一题选什么由本地状态机/策略函数决定,输入是地图 + 近期事件序列;LLM 不参与决策,只参与执行。
3. **AI 是维护工,不是讲师**。AI 维护地图、顺着地图出题;用户是地图的主人(可随时查看、手动校准)。
4. **每个信号都要有系统响应**。无聊、跳过、追问、改答案……任何信号落入系统后必须在可预期时间内改变出题行为(见 §9 对照表)。
5. **事件日志是唯一真正的资产**。模型(GPT / Claude / Gemini / 本地模型)只是可替换的"老师与评估器";属于用户的资产是跨周跨月的结构化学习事件——§14 三类诊断正来自它,这是对话式 AI 长期稳定给不出的东西。

---

## 2. 核心概念与数据模型

### 2.1 概念层级

```
User ──► Domain(领域,如 透视/音乐/文学)
             └── SkillTree:SkillNode 树(技能节点,父子结构)
                     └── SkillState:该节点对该用户的当前掌握度(认知地图的"节点值")

Event:一次性交互记录,只增不改
Question:题目(带 node_id、difficulty、format),可复用、可 QC
Session:一次学习会话,事件按 session 归组
```

### 2.2 SkillNode(技能节点)

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 如 `perspective.dynamic_pose` |
| `domain` | string | 所属领域 |
| `name` | string | 展示名,如 "动态姿势" |
| `parent` | string/null | 父节点 id |
| `children` | string[] | 子节点 id |
| `prereqs` | string[] | 可选,前置节点(影响推荐顺序,不影响判定) |

### 2.3 SkillState(认知地图节点值)

| 字段 | 类型 | 说明 |
|---|---|---|
| `mastery` | 0–100 | 掌握度估计 |
| `evidence_count` | int | 该节点被检验次数(计数越多更新越保守) |
| `last_seen` | timestamp | 最近一次交互时间 |
| `correct_streak` | int | 近期连续答对数(用于升难度) |
| `wrong_streak` | int | 近期连续答错数(用于降难度/回退) |
| `skip_streak` | int | 近期连跳数(无聊检测) |
| `followup_streak` | int | 近期主动追问数(兴趣检测) |
| `difficulty_bias` | int | 当前推荐难度相对 mastery 的偏移(初始 0,策略函数负责升降) |

> v1 中 streak 字段只按最近 N=5 相关事件计算,不存历史。

### 2.4 初始技能树(seed,取自原始需求)

```
透视(perspective)
├── perspective.basic_space    基础空间关系(85)
├── perspective.occlusion      遮挡关系(78)
├── perspective.proportion     人体比例(61)
└── perspective.dynamic_pose   动态姿势(37) ← 子技能最低洼,冷启动后的重点对象

音乐(music)
├── music.beat                 节拍(92)
├── music.rhythm               节奏(82)
├── music.harmony_function     和声功能(64)
├── music.melody_expectation   旋律期待(71)
└── music.orchestration        配器(48)

文学(literature)
├── literature.pov             叙事视角(76)
├── literature.imagery         意象(72)
├── literature.metaphor        隐喻(83)
├── literature.structure       结构(61)
└── literature.lang_rhythm     语言节奏(54)
```

> 上述百分数是原始需求的**示例值**,仅用来说明地图形态;真实用户一律走 §8 冷启动。
> 领域树是配置数据(`domains/*.yml`),不写死在代码里;树的生长/打细是 v2 用戶参与的演进项。

### 2.5 Question(题目)

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | `q_...` |
| `node_id` | string | 归属技能节点(必填,LLM 生成时不可缺省) |
| `difficulty` | 0–100 | 难度,与 mastery 同尺度 |
| `kind` | enum | `concept`(概念直测)/ `variant`(变式)/ `transfer`(陌生场景迁移),默认 `concept`;§14.1 迁移诊断依赖此字段 |
| `format` | enum | `mcq`(选择)/ `free`(简答)/ `explain`(说出理由) |
| `body` | object | 题面、选项、素材引用 |
| `answer_key` | object | 判分依据(mcq 选项 / free 的评分点列表 rubric) |
| `source` | enum | `seed / llm / user` |
| `qc_status` | enum | `draft`(LLM 新生成)/ `approved`(可出题) |

---

## 3. 事件模型(地图的唯一输入)

### 3.1 事件类型

| type | 必要字段 | 语义 |
|---|---|---|
| `answer_correct` | question_id, node_id, difficulty | 判分通过 |
| `answer_wrong` | question_id, node_id, difficulty | 判分不通过 |
| `changed_answer` | question_id, from, to(可选 `reason`) | 改答案 = 犹豫信号;mastery 更新打 0.5 折(随对/错一起结算) |
| `skip` | question_id(可选 `reason`: too_easy / too_hard / boring) | 跳过;秒答(≤5s)+ 跳过也计 boring |
| `pause` | question_id, dwell_ms | 长停留**不扣分**——停留是思考不是错误,仅更新 last_seen |
| `boredom_flag` | question_id(可选 text) | 用户显式点"无聊" |
| `followup` | question_id(可选 text) | 用户主动追问 → 最重要的兴趣信号 |
| `retention_check` | 同 answer_* | 隔天/隔周的复习检验,按普通答题结算 |
| `self_report` | node_id, self_rating(0–100) | 用户自评,仅作弱先验校正(§8) |

`answer_*` 事件 payload 可含 `self_confidence`(1=瞎猜 / 2=不太确定 / 3=大概率对 / 4=确定,作答时顺手点选)与 `mastery_after`(结算快照)——v1 只记录不使用,是 §14.2 / §14.3 报表的原料。
注意:原始作答文本的判分过程**不进事件流**;事件流只存已判分结果(§6 判分通道)。

### 3.2 事件 JSON 示例

```json
{
  "id": "ev_01H8...",
  "ts": "2025-09-26T12:00:00Z",
  "session_id": "s_...",
  "type": "answer_wrong",
  "question_id": "q_7f2",
  "node_id": "perspective.dynamic_pose",
  "difficulty": 55,
  "payload": { "dwell_ms": 41000, "changed_from": 3 }
}
```

### 3.3 事件流性质

- **只增不改**:后续修正以新事件(`self_report`)表达,v1 不提供删除。
- **可重放**:`events.log` 按序重放必须能重建与当前一致的地图状态(§11 验收)。
- v1 本地单用户;`user_id` 字段保留在 schema 中,恒为本地唯一值。

---

## 4. 掌握度更新算法(SkillState 更新)

v1 不用复杂贝叶斯知识追踪,选**可解释的 Elo 风格更新**:验收要求是"同一事件序列重放得到同一张地图",而非统计最优。

### 4.1 公式

```
expected   = clip((state.mastery - difficulty) / 50 + 0.5, 0.05, 0.95)
observed   = 1.0(answer_correct)| 0.0(answer_wrong)
K(e)       = 40 / (1 + log2(1 + evidence_count))

weight     = 1.0;若该题结算前发生 changed_answer → weight = 0.5
state.mastery = clip(state.mastery + K * weight * (observed - expected), 0, 100)
state.evidence_count += 1
state.last_seen = now
```

配套 streak 更新:答对 → `correct_streak+=1, wrong_streak=0`;答错反之;`skip / boredom_flag` → `skip_streak`;`followup` → `followup_streak`。

要点:

- 难度与 mastery 同为 0–100 尺度;`expected` 是"这题本来该不该答对"。错得越意外,mastery 掉得越多;赢得越意外涨得越多。
- `K` 随证据量衰减:前几次交互快速校准,老节点不会又被极端事件拉扯。
- **v1 不做遗忘衰减**:地图数值不因时间流逝而变;"该复习了"完全由 §5 的 recency 权重表达。避免凭空蒸发用户涨上来的掌握度。

### 4.2 难度偏差(difficulty_bias)的演变

| 条件(该节点最近 N=5 事件) | 动作 |
|---|---|
| `correct_streak ≥ 3` | `difficulty_bias += 15` |
| `wrong_streak ≥ 2` | `difficulty_bias -= 10`;若有效难度已低于节点难度下限 → **回退**:推荐 mastery 更低的兄弟技能或父节点 |
| `skip(too_easy)` 或 ≤5s 正确作答 | `difficulty_bias += 15`,或把 next-pointer 指向 mastery 最低的子节点 |
| `skip(boring)` / `boredom_flag` | 该节点临时降权(§5),`difficulty_bias += 10` |

---

## 5. 下一题策略(核心决策)

### 5.1 节点选择:评分函数

每次出题,对候选节点(node)打分取最高:

```
score(node) =
    w_rec  * norm(days_since(node.last_seen))        # 间隔:越久没碰越优先
  + w_zone * zone_fit(node.mastery)                 # 目标区贴合
  + w_below* gap_bonus(node)                        # 树形补短:优先照顾低洼子节点
  + w_intr * node.followup_streak                   # 兴趣:刚被追问过的节点加权
  - w_bore * (node.skip_streak + bored_flag_recent) # 无聊:近期被嫌弃者降权
```

- `zone_fit`:mastery >85 或 <40 的节点降权(默认作战区 40–90,可配置)。
- `gap_bonus`:倾向选择"父节点已过半但有低洼子节点"的范围(如 37% 的动态姿势)。
- v1 初值:`w_rec=20, w_zone=20, w_below=25, w_intr=15, w_bore=30`;后续按真实事件调参。

### 5.2 难度选择(节点定了之后)

```
target_difficulty = clamp(state.mastery + state.difficulty_bias, 15, 95)
```

- 优先从题库选 `|q.difficulty − target|` 最小、且近 10 次未出过"同 node 同 format"的题。
- 题库不足 → 调 LLM 按 `(node_id, target_difficulty, format, 近期已出题摘要)` 现场生成,走 §6.3 QC 轨道。
- **每题响应必须带 `reason` 字段**(为什么出这道题),用户能看到意图,建立对地图的信任。

### 5.3 深挖模式(dive mode,兴趣爆发驾驭)

触发:同一 session 内,同一节点(或其祖先)`followup_streak ≥ 2`。

- 进入:`dive.node` 置为该节点;策略被覆盖:下一题限定 `subtree(dive.node)`,难度偏 `mastery+20`(挑战区),题型偏 `explain/free`。
- 退出:`boredom_flag`、同 session 连续 3 题 skip、或用户显式说"回来正常学"。
- 深挖期间的作答同样走 §4 掌握度更新——**深挖也是学习**,不只是消遣。

### 5.4 防重复

- 同一题 14 天内不重复出(除 `retention_check` 调度)。
- 非 dive、非低洼节点时,同节点连续两次出题之间必须先出其他节点一次,且轮换题型 format。

---

## 6. LLM 职责边界(最重要的架构决定)

### 6.1 可以做

| 职责 | 输入/输出 |
|---|---|
| 出题生成 | 输入 `(node_id, target_difficulty, format, 近期已出)`,输出 Question(draft) |
| 判分 | 输入 `(question, user_answer)`,输出 `{correct:0/1, confidence}`;confidence ≥ 0.8 直接落事件,否则请用户自验后确认 |
| 解释 | 答错后按节点状态/难度偏差讲评 |
| 追问引导 | followup 后按 dive/challenge 输出追问 mini 挑战 |
| 语义抽取 | 用户自由文本若含地图语义(如"这部分我其实会了")→ 抽取为标准事件,**先预览,用户确认后落** |

### 6.2 硬约束(不许做)

- ❌ 直接读写 mastery / SkillState——只有事件管线(§4)能写。
- ❌ 自行决定"下一题是哪个节点"——策略函数(§5)唯一决策。
- ❌ 未经 QC 的题目直接入库出题(§6.3)。

### 6.3 LLM 题目 QC 入轨

1. LLM 产出 draft(必须带 node_id + difficulty)。
2. 校验器:题面与节点语义粗筛、难度与目标差、answer_key 可判分。
3. draft 首次被成功作答判分后自动 `approved`;被判(qc miss)→ 退回 draft 并登记。
4. 全部 draft/approved 可审计,保证"这题到底在测什么技能"可追溯。

---

## 7. API 设计 v1(REST/JSON,本地单进程即可)

| Method | Path | 请求/响应要点 |
|---|---|---|
| POST | `/v1/sessions` | `{domain}` → `{session_id}` |
| GET | `/v1/next?session=…` | → `{question, node, difficulty, reason, dive_active}`(策略主入口) |
| POST | `/v1/answers` | `{question_id, answer, dwell_ms?, changed_from?}` → 判分 → 落事件 → `{node_snapshot, nudge}` |
| POST | `/v1/skips` | `{question_id, reason?}` → skip 事件 |
| POST | `/v1/followups` | `{question_id, text}` → followup + dive 计数 → `{reply, challenge_question?}` |
| POST | `/v1/boredoms` | `{question_id, text?}` → boredom_flag → 立即返回新下一题 |
| GET | `/v1/map?domain=…` | 整棵技能树 + 各节点 SkillState(认知地图可视化数据) |
| POST | `/v1/map/self-calibrate` | `{node_id, self_rating}` 弱先验校正(§8) |
| POST | `/v1/events` | 直接注入事件(脚本/自动化/高级调试) |
| GET | `/v1/events` | 按 session/domain 过滤导出 |
| POST | `/v1/questions/drafts` | 手动触发 LLM draft(默认不入题库) |
| POST | `/v1/questions/{id}/approve` | 题目 approve |
| GET | `/v1/replay` | 重放事件给出地图逐步变化(审计/复盘) |
| GET | `/v1/diagnostics` | §14 三类报表(迁移差距 / 保持曲线 / 自信校准);纯离线只读,不回流状态 |

判分响应示例:

```json
{
  "judge": { "correct": false, "confidence": 0.93 },
  "events": ["answer_wrong(weight 0.5, changed from 3)"],
  "node_snapshot": {
    "node_id": "perspective.dynamic_pose",
    "mastery": 35.8, "previous": 38.2,
    "correct_streak": 0, "wrong_streak": 1
  },
  "nudge": "错了——难度 55,低于预期,下次以 45 难度再试(节点:动态姿势)。"
}
```

`nudge`:每次地图变动都给用户一句话透明反馈(为何升/降)。

---

## 8. 冷启动与校准

1. **Placement quiz**(新用户 per domain):分层取样 5 节点 × 1 题(先验按 40/50/60 阶梯),对/错初始化:
   - 对 → `mastery = clamp(q.difficulty + 10, 40, 90)`
   - 错 → `mastery = clamp(q.difficulty - 15, 10, 60)`
2. **自评表**:放置后允许用户对每个领域点一次自评,映射为各节点先验(上限 70);在节点 `evidence_count ≥ 3` 前自评事件权重 0.3,之后完全被真实事件覆盖。
3. **最小证据**:节点 `evidence_count < 3` 时地图显示"估"标记。

---

## 9. 信号 → 响应对照表(索引)

| 原始行为信号 | 事件 | 系统响应 |
|---|---|---|
| 太简单 → 无聊 | `skip(too_easy)` / 秒答 | bias +15 或跳到 mastery 最低子节点;同 format 连续禁出 |
| 太重复 → 跳过 | `skip(boring)` | §5.4 防重复;换 node / 换题型 |
| 突然感兴趣 → 深挖 | `followup ×2` | dive mode:子树限定 + 挑战难度 + explain 型题 |
| 真难的地方愿意停下来思考 | `pause`(高 dwell) | 不扣分、只更 last_seen;答错照常算 wrong_streak(思考≠避免错误) |
| 犹豫/改答案 | `changed_answer` | mastery 折半记账 + 复盘记录 |
| 卡住 | `wrong_streak ≥ 2` | 难度 −10 或回退父节点/最简子技能;给出 explanation |
| 显式"这好无聊" | `boredom_flag` | 该节点临时降权,立刻换题 |
| 很久没碰某技能 | last_seen 久 | recency 加权 → 优先出 retention 题 |

> 原则:任何信号落入系统都要在可预期时间内改变出题行为——**地图有体感**。

---

## 10. 存储与建议实现栈

- 存储:SQLite 单文件 `myteacher.db`;表:`nodes / states / questions / events(append-only) / sessions / drafts`。
- 另外可选落一行一个 JSON 的 `events.log` 供重放与备份。
- 栈建议(不强约束):Python 3.11+ / FastAPI + SQLite;LLM 适配层可插拔(OpenAI-compatible HTTP);前端 v1 用 CLI/聊天壳即可,重点在后端状态机。
- 模块划分:`store / events / policy / llm / judge / replay`。

---

## 11. 不变量与验收标准

1. **重放一致性**:同一 `events.log` 顺序重放 → 地图状态逐节点相同(含 streak、bias)。
2. **LLM 隔离**:mock LLM 返回"把全部 mastery 置 0"的恶意输出 → 地图不受影响;LLM 只能进 drafts/判分/解释通道。
3. **反无赖制造机**:最近 5 事件 skip ≥ 3 → 下一题必换节点、或难度变化 ≥ 15。
4. **兴趣优先**:同 session 同节点 followup ≥ 2 → `dive_active=true`,下一题落在 `subtree(dive.node)`。
5. **回退路径**:`wrong_streak ≥ 2` 固定输入 → 推荐必为父节点或更低 mastery 兄弟。
6. **判分一致性**:mcq 确定性判分;free 判分 confidence 必须记录,低置信先请用户自验。
7. **透明反馈**:每个写事件响应都含 `nudge`(节点名 + 方向 ↑/↓ + 原因)。
8. **诊断可算性**:导出的每条 `answer_*` 事件都携带 `kind`(join 题目)、`self_confidence`(可选)、`mastery_after` 快照,§14 三类指标能离线稳定算出(字段完整性 fixtures)。

---

## 12. 里程碑

- **M1 骨架**:SkillTree + SkillState + 事件 + Elo 更新 + 策略 + next + SQLite + fixtures 自动验收(§11.1–5)。LLM 用确定性 stub,先跑通状态机。
- **M2 LLM 入座**:题目 draft 生成、判分、explain、QC 入轨(§6)。
- **M3 体验**:聊天壳 + `/v1/map` 认知地图可视化 + nudge 文案。
- **M4 调参 + 诊断报表**:用真实事件调 §5 权重;上线 §14 三类报表(迁移差距 / 保持曲线 / 自信校准);v2 候选(Transfer 策略接线、遗忘漂移、树自动细分、多领域并行)。

---

## 13. 非目标(明确不做)

- ❌ AI 讲授课程 / 不出课程大纲
- ❌ 多用户/账号/班级体系(v1 本地单用户)
- ❌ 勋章、积分、排行榜
- ❌ 学习时长报告、周报
- ❌ 无用户参与地自动加新领域(树的演进需用户参与)

---

## 14. 数据派生指标(v1 只存原料,报表 v2 才出)

> 来自补充讨论:跑 1–2 个月、攒够真实数据后再启用。v1 的责任是**不丢数据**——§2.5 的 `kind`、§3 的 `self_confidence` 就是为这三类报表预留的字段。
> 有意保留在 v1 的例外:Interest/Boredom 不进本表——它们是本产品的起点动机,且只是两类事件 + 两个策略权重(§5 / §9),不构成建模负担。

### 14.1 迁移诊断(Transfer)

- 输入:`answer_*` 事件 join `question.kind`。
- 输出:per node × kind 正确率对比(如 概念直测 91% / 变式 63% / 陌生场景 48%)。
- 结论模板:"缺的不是理解,是迁移。"(v2 接线:策略把题型向 transfer 倾斜)

### 14.2 保持曲线(Retention)

- 输入:事件时点 mastery、days_since 上次交互、判分结果。
- 输出:1/7/30 天分桶正确率(如 91% → 67% → 42%)。
- 结论模板:"理解没问题,长期保持不足。"(v2 决定是否引入遗忘漂移与复习计划)

### 14.3 自信校准(Confidence)

- 输入:`self_confidence` vs 判分结果。
- 输出:校准对比(如 confidence 94% vs accuracy 58% = 系统性高估)。
- 结论模板:"高估了掌握程度。"(v2 可据此放慢 difficulty_bias 的上升速度)

### 14.4 实现注意

- 三者均为 events.log 的纯离线分析,不回流地图状态(除非 v2 明确接线);不新增表、不新增状态变量。
- 已上线:`GET /v1/diagnostics` 端点与 `myteacher/tune.py` 网格 harness(§12 M4)。

---

## 15. 开放问题

1. `pause` 要不要主动问"你在想什么"?(v1 默默记录,仅在 dive 时引导追问)
2. `changed_answer` 是否强制 `reason`?(v1 可选,故 0.5 折)
3. mastery 是否附 volatility/CI 显示(展示"估算不确定度")?(v2 候选)
4. 遗忘漂移要不要进 v2?若做:仅作用于 >14 天未检验的节点,单日衰减 ≤5,retention 检验撤销。
5. 多 domain 并行会话时,dive 是否跨 domain 记忆?
6. seed 题库最小可用集:每 node 目标难度 ±15 覆盖,多少题才够(暂定每节点 15 题,否则依赖 LLM )?
7. free 判分 rubric 谁维护?(建议:生成题时 LLM 连 rubric 一起产出,人工 approve)
8. self_confidence 用四档还是 0–100?(四档省心;百分制能算准校准曲线 §14.3)
