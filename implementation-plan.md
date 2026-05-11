# Harness 实现方案：三节点架构

## 目标

在现有四阶段流水线（intent → manager → expert → reporter）中插入三个通用 harness 节点，解决：
1. **Verify Loop**：intent 阶段产生的 RiskItem 必须有代码证据锚点，否则不进入后续流程
2. **Eval Gate**：专家输出必须经过独立质量门验证，不能仅靠 LLM 自评 confidence
3. **Progressive Context**：Prompt 拆为核心指令 + 按需加载的模式库，避免注意力涣散

---

## 流水线变更

**变更前**：
```
intent_router → intent_analysis → manager → expert_execution → reporter
```

**变更后**：
```
intent_router → intent_analysis → verify_loop → manager → expert_execution → eval_gate → reporter
```

Progressive Context 不新增节点，而是修改现有 prompt 的组织和加载方式。

---

## 第一步：Verify Loop 节点 — 代码证据锚点验证

### 1.1 创建 prompt 模板文件

- 新建文件 `agents/prompts/verify_loop.txt`
- 文件内容需定义：
  - 角色：你是一个"代码证据验证器"，不做深度分析，只验证 RiskItem 是否指向了具体的代码行
  - 输入占位符：`{file_path}`、`{file_content}`、`{risk_description}`、`{risk_type}`、`{line_number}`
  - 任务：读取 `line_number` 指定的代码行，判断该位置的代码是否**直接支撑** `risk_description` 所描述的问题
  - 输出格式：严格 JSON，字段为 `anchored: bool`（是否锚定到具体代码）、`evidence: string`（找到或没找到什么证据）、`adjusted_confidence: float`（如果 anchored 为 true 则保持原 confidence，否则降至 0.3）
  - 约束：不调用外部工具，只基于已提供的 `file_content` 做判断；如果 `line_number` 超出 `file_content` 范围，直接标记为未锚定

### 1.2 创建 verify_loop 节点函数

- 新建文件 `agents/nodes/verify_loop.py`
- 函数签名：`async def verify_loop_node(state: ReviewState) -> Dict[str, Any]`
- 逻辑：
  1. 从 `state["file_analyses"]` 中提取所有 `RiskItem`
  2. 对每个 `RiskItem`，用 `read_file_content` 读取其 `file_path` 的内容
  3. 用 `render_prompt_template("verify_loop", ...)` 组装 prompt
  4. 调用 LLM 做验证（`llm.ainvoke`）
  5. 解析返回的 JSON，对 `anchored=False` 的 RiskItem 将其 confidence 降至 0.3
  6. 返回更新后的 `file_analyses`
- 并发控制：使用 `asyncio.Semaphore`，并发数从 `config.system.max_concurrent_llm_requests` 读取
- 进度输出：每处理 5 个 RiskItem 打印一次进度

### 1.3 将 verify_loop 接入工作流

- 修改 `agents/workflow.py`：
  - import `verify_loop_node`
  - 在 `create_multi_agent_workflow` 中添加节点：`workflow.add_node("verify_loop", verify_loop_node)`
  - 添加边：`workflow.add_edge("intent_analysis", "verify_loop")` 和 `workflow.add_edge("intent_analysis_chunked", "verify_loop")`
  - 修改原有边：将 `intent_analysis → manager` 改为 `verify_loop → manager`，chunked 同理
  - 在路由函数 `route_to_intent` 的目标字典中添加 verify_loop

### 1.4 验证测试

- 在 `test/` 目录下新建 `test_verify_loop.py`
- 测试 1（单元测试）：构造一个包含 2 个 RiskItem 的 ReviewState（一个锚定到真实变更行，一个锚点到不存在的行），调用 `verify_loop_node`，验证未锚定的 RiskItem confidence 被降至 ≤0.3
- 测试 2（集成测试）：使用 `dataset/sentry-greptile` 中已有的一个 PR diff，运行完整流水线到 verify_loop 节点，打印输出，人工确认被过滤的 RiskItem 确实是无法锚定的
- 验证标准：测试 1 通过；测试 2 中 verify_loop 过滤掉的 RiskItem 数量 > 0，且被过滤项在原始 intent 输出中存在

---

## 第二步：Eval Gate 节点 — 独立质量门

### 2.1 创建 prompt 模板文件

- 新建文件 `agents/prompts/eval_gate.txt`
- 文件内容需定义：
  - 角色：你是一个"独立评审员"，任务是交叉验证另一个专家的分析结论
  - 输入占位符：`{file_path}`、`{file_content}`、`{diff_context}`、`{expert_conclusion}`（RiskItem 的 description）、`{risk_type}`、`{expert_confidence}`
  - 任务：
    1. 将专家结论改写为一个可证伪断言（"如果 X 成立，则在代码 Y 处应观察到 Z"）
    2. 从**与专家不同的角度**寻找证据：如果专家说的是"缺少判空"，你就去找"是否存在判空"；如果专家说"会抛 KeyError"，你就去找"该 key 是否确实存在"
    3. 如果找到反证，标记为 `disputed`；如果没找到反证但证据也不足，标记为 `uncertain`；如果确认专家结论，标记为 `confirmed`
  - 输出格式：严格 JSON，字段为 `verdict: string`（"confirmed" / "disputed" / "uncertain"）、`reason: string`（一句话说明）、`final_confidence: float`（confirmed 则取 max(专家confidence, 0.7)，disputed 则降至 0.3，uncertain 则取专家confidence 的 0.8 倍）

### 2.2 创建 eval_gate 节点函数

- 新建文件 `agents/nodes/eval_gate.py`
- 函数签名：`async def eval_gate_node(state: ReviewState) -> Dict[str, Any]`
- 逻辑：
  1. 从 `state["expert_results"]` 中提取所有 RiskItem（按风险类型分组）
  2. 对每个 RiskItem，读取其 `file_path` 的完整内容和该文件的 diff 片段
  3. 用 `render_prompt_template("eval_gate", ...)` 组装 prompt
  4. 调用 LLM 做独立验证
  5. 解析返回的 JSON，根据 `verdict` 调整 confidence
  6. 返回更新后的 `expert_results`
- 并发控制：同 verify_loop，使用 Semaphore
- 进度输出：按风险类型分组打印验证结果摘要（每个类型一行：confirmed/disputed/uncertain 数量）

### 2.3 将 eval_gate 接入工作流

- 修改 `agents/workflow.py`：
  - import `eval_gate_node`
  - 添加节点：`workflow.add_node("eval_gate", eval_gate_node)`
  - 修改边：将 `expert_execution → reporter` 改为 `expert_execution → eval_gate → reporter`

### 2.4 验证测试

- 在 `test/` 目录下新建 `test_eval_gate.py`
- 测试 1（单元测试）：构造 expert_results，包含一个高置信度但实际为误报的 RiskItem（例如"Prisma 非空字段可能为 null"，但实际 schema 中该字段是 `String @nonNull`），调用 `eval_gate_node`，验证其 confidence 被显著降低
- 测试 2（集成测试）：使用 `dataset/cal.com-greptile` 中已有的一个 PR diff，运行完整流水线到 eval_gate 节点，对比 eval_gate 前后的 RiskItem 数量和 confidence 分布
- 验证标准：测试 1 中误报项 confidence 降至 ≤0.4；测试 2 中 eval_gate 至少对 1 个 RiskItem 的 verdict 为 "disputed" 或 "uncertain"

---

## 第三步：Progressive Context — Prompt 动态组装

### 3.1 拆分 intent_analysis.txt

- 将现有的 `agents/prompts/intent_analysis.txt` 拆分为：
  - `agents/prompts/intent_core.txt`：只保留角色定义、输入格式、输出要求（JSON 格式、行号格式）、"必须锚定到变更"规则、风险合并规则
  - `agents/prompts/patterns/robustness.txt`：健壮性与边界条件的定义 + 5 个危险模式
  - `agents/prompts/patterns/concurrency.txt`：并发与时序正确性的定义 + 5 个危险模式
  - `agents/prompts/patterns/authorization.txt`：鉴权与数据暴露的定义 + 4 个危险模式
  - `agents/prompts/patterns/intent.txt`：需求意图与语义一致性的定义 + 案例映射
  - `agents/prompts/patterns/lifecycle.txt`：生命周期与状态一致性的定义 + 5 个危险模式 + 5 个危险模式

### 3.2 实现动态模式检测器

- 新建文件 `util/pattern_detector.py`
- 函数签名：`def detect_patterns_from_diff(diff_content: str) -> List[str]`
- 逻辑：
  1. 对 diff 内容做简单的关键词匹配（不依赖 LLM）：
     - 如果出现 `async`、`forEach`、`map(`、`Promise`、`await`、`Thread`、`Lock`、`synchronized`、`goroutine`、`channel` → 标记 "concurrency"
     - 如果出现 `if (`、`.get(`、`?.`、`|| `、`?? `、`null`、`undefined`、`None`、`try`、`catch`、`except` → 标记 "robustness"
     - 如果出现 `sql(`、`exec(`、`eval(`、`fetch(`、`axios`、`password`、`token`、`secret`、`isAdmin`、`hasPermission` → 标记 "authorization"
     - 如果出现 `update(`、`delete(`、`setInterval`、`subscribe`、`useEffect`、`def func(` 且参数含可变默认值 → 标记 "lifecycle"
     - 如果出现布尔逻辑运算符嵌套（`&&` 和 `||` 混用）、时间计算、比较逻辑 → 标记 "intent"
  2. 返回检测到的模式列表（去重）
  3. 如果未检测到任何模式，默认返回所有模式（保守策略）

### 3.3 修改 intent_analysis_node 的 prompt 组装逻辑

- 修改 `agents/nodes/intent_analysis.py` 中的 `analyze_file` 函数：
  1. 在渲染 prompt 之前，调用 `detect_patterns_from_diff(file_diff)` 获取当前文件相关的模式
  2. 加载 `intent_core.txt` 作为基础 prompt
  3. 对每个检测到的模式，加载对应的 `patterns/{pattern}.txt`，追加到基础 prompt 之后
  4. 用组装后的完整 prompt 调用 LLM
- 修改 `agents/nodes/intent_analysis_chunked.py`，做同样的改动

### 3.4 同样拆分 expert prompt

- 对 `expert_Robustness_Boundary_Conditions.txt`，拆分为：
  - 核心部分保留在原文件：角色定义、分析流程、置信度标尺、工具约束、指导原则
  - "第三方 API 契约速查"部分移到 `agents/prompts/references/` 目录（如 `references/django_orm.txt`、`references/java_optional.txt`）
- 修改 `agents/expert_graph.py` 中的 reasoner 节点：在构建专家 prompt 时，根据 diff 中是否出现 ORM 查询/Optional 使用等关键词，决定是否注入参考文件内容

### 3.5 验证测试

- 在 `test/` 目录下新建 `test_progressive_context.py`
- 测试 1（单元测试）：构造几个 diff 片段（纯 CSS 变更、含 async 的 JS 变更、含 SQL 拼接的 Python 变更），调用 `detect_patterns_from_diff`，验证返回的模式列表正确
- 测试 2（单元测试）：构造一个只含 async/forEach 变更的 diff，验证 intent_analysis_node 组装的 prompt 中**不包含** lifecycle 和 intent 模式的详细定义
- 测试 3（集成测试）：使用一个真实 PR diff 运行 intent_analysis，在日志中打印检测到的模式和组装后的 prompt 长度，对比改造前（固定 200 行）和改造后（动态行数），验证 prompt 长度减少
- 验证标准：测试 1 所有模式检测正确；测试 2 中 prompt 不包含无关模式定义；测试 3 中 prompt 行数比改造前减少至少 30%

---

## 第四步：配置项与回退机制

### 4.1 在 config.yaml 中添加 harness 配置

- 在 `config.yaml` 的 `system` 段下新增：
  - `harness_verify_loop_enabled: true` — 是否启用 verify_loop
  - `harness_eval_gate_enabled: true` — 是否启用 eval_gate
  - `harness_progressive_context_enabled: true` — 是否启用动态 prompt 组装
  - `harness_verify_confidence_floor: 0.3` — verify_loop 对未锚定项的 confidence 下限

### 4.2 在 workflow 中根据配置条件性插入节点

- 修改 `create_multi_agent_workflow`：
  - 在添加 verify_loop 节点前，检查 `config.system.harness_verify_loop_enabled`
  - 如果为 false，则保持原有边 `intent_analysis → manager`
  - eval_gate 和 progressive_context 同理

### 4.3 验证测试

- 不需要独立测试文件，在已有的 `test_verify_loop.py` 和 `test_eval_gate.py` 中各加一个测试：
  - 将对应配置项设为 false，运行流水线，验证该节点被跳过（输出中不出现该节点的日志）

---

## 第五步：端到端验证

### 5.1 使用已有测试数据集跑一轮

- 使用命令 `python test/run_automated_tests.py --repos sentry --cases 1-3` 运行 3 个 sentry 测试用例
- 收集输出日志和 review_results，与 `result_1.md` 中的基线结果对比
- 验证标准：
  - FP 数量相比基线减少（特别是 Robustness 类的"过度防御建议"）
  - FN 数量不增加（verify_loop 和 eval_gate 只过滤，不新增 RiskItem）
  - 总运行时间增加不超过 30%（每个 RiskItem 多一次 LLM 调用）

### 5.2 记录结果

- 将本轮运行结果写入 `result_2.md`（与 `result_1.md` 同格式）
- 对比 result_1.md 和 result_2.md，统计 TP/FN/FP 变化

---

## 文件变更汇总

| 操作 | 文件路径 | 说明 |
|------|----------|------|
| 新建 | `agents/prompts/verify_loop.txt` | Verify Loop prompt 模板 |
| 新建 | `agents/prompts/eval_gate.txt` | Eval Gate prompt 模板 |
| 新建 | `agents/nodes/verify_loop.py` | Verify Loop 节点实现 |
| 新建 | `agents/nodes/eval_gate.py` | Eval Gate 节点实现 |
| 新建 | `agents/prompts/intent_core.txt` | Intent Analysis 核心指令 |
| 新建 | `agents/prompts/patterns/robustness.txt` | 健壮性模式定义 |
| 新建 | `agents/prompts/patterns/concurrency.txt` | 并发模式定义 |
| 新建 | `agents/prompts/patterns/authorization.txt` | 鉴权模式定义 |
| 新建 | `agents/prompts/patterns/intent.txt` | 意图语义模式定义 |
| 新建 | `agents/prompts/patterns/lifecycle.txt` | 生命周期模式定义 |
| 新建 | `util/pattern_detector.py` | Diff 模式检测器 |
| 修改 | `agents/workflow.py` | 插入 verify_loop 和 eval_gate 节点 |
| 修改 | `agents/nodes/intent_analysis.py` | 动态组装 prompt |
| 修改 | `agents/nodes/intent_analysis_chunked.py` | 动态组装 prompt |
| 修改 | `config.yaml` | 添加 harness 配置项 |
| 新建 | `test/test_verify_loop.py` | Verify Loop 测试 |
| 新建 | `test/test_eval_gate.py` | Eval Gate 测试 |
| 新建 | `test/test_progressive_context.py` | Progressive Context 测试 |
