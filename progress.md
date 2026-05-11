# 实现进度记录

## Step 1: Verify Loop 节点（已完成）

### 做了什么

**新增文件**：
- `agents/prompts/verify_loop.txt` — Verify Loop 的 prompt 模板，定义证据锚点验证规则和 confidence 调整策略
- `agents/nodes/verify_loop.py` — Verify Loop 节点实现，并发验证所有 RiskItem 的代码证据锚点
- `test/test_verify_loop.py` — 5 个单元测试 + 1 个集成测试（跳过）

**修改文件**：
- `agents/workflow.py` — 插入 verify_loop 节点（intent_analysis → verify_loop → manager），同时预留 eval_gate 节点占位
- `agents/nodes/eval_gate.py` — 新建占位文件（直通 passthrough），step 2 将完整实现

### 核心逻辑

Verify Loop 节点在 intent_analysis 和 manager 之间执行：
1. 从 `state["file_analyses"]` 提取所有 RiskItem
2. 对每个 RiskItem，读取其 file_path 内容，调用 LLM 验证 line_number 处是否有直接代码证据
3. 锚定标准：行号在范围内 + 代码内容与风险描述语义相关 + 描述中的符号在代码中可见
4. 未锚定 → confidence 降至 0.3（可通过 `harness_verify_confidence_floor` 配置）
5. LLM 调用失败 → confidence 降至原值 0.9 倍（不低于 0.3）

### 测试结果

- 5/5 单元测试通过
- 1 集成测试跳过（需设置 `RUN_INTEGRATION_TESTS=1`）
- workflow 导入验证通过

### 后续开发者注意

- eval_gate.py 当前是占位实现（直通），step 2 需要完整实现后替换
- verify_loop 的 prompt 直接内联在 `_build_verify_prompt()` 中而非使用 `render_prompt_template`，是为了节省每次调用的文件 IO 开销

---

## Step 2: Eval Gate 节点（已完成）

### 做了什么

**新增文件**：
- `agents/prompts/eval_gate.txt` — Eval Gate 的 prompt 模板，定义独立交叉验证规则和三档 verdict
- `test/test_eval_gate.py` — 8 个单元测试（disputed/confirmed/uncertain/混合/空结果/LLM 错误/confidence 范围/多风险类型）

**修改文件**：
- `agents/nodes/eval_gate.py` — 从占位实现替换为完整实现

### 核心逻辑

Eval Gate 节点在 expert_execution 和 reporter 之间执行：
1. 从 `state["expert_results"]` 提取所有 RiskItem（按风险类型分组）
2. 对每个 RiskItem，读取其 file_path 内容和 diff 片段，调用 LLM 做独立验证
3. 验证逻辑：将专家结论改写为可证伪断言 → 从不同角度搜索反证 → 判定 verdict
4. 三档调整：
   - `confirmed` → confidence = max(专家confidence, 0.7)
   - `disputed` → confidence = 0.3
   - `uncertain` → confidence = 专家confidence × 0.8
5. LLM 调用失败 → confidence 降至原值 0.95 倍（不低于 0.3）

### 测试结果

- 8/8 单元测试通过
- 与 Step 1 合计 13/13 通过（1 集成测试跳过）
- workflow 导入验证通过

### 后续开发者注意

- eval_gate 使用 `_VERDICT_MAP` 字典映射 verdict 到 confidence 调整函数，新增 verdict 类型只需在此字典添加条目
- 与 verify_loop 一样，prompt 内联而非使用模板文件，减少 IO 开销

---

## Step 3: Progressive Context — Prompt 动态组装（已完成）

### 做了什么

**新增文件**：
- `agents/prompts/intent_core.txt` — Intent Analysis 核心指令（角色定义、输出格式、行号格式、锚定规则、风险合并规则）
- `agents/prompts/patterns/robustness.txt` — 健壮性与边界条件模式定义
- `agents/prompts/patterns/concurrency.txt` — 并发与时序正确性模式定义
- `agents/prompts/patterns/authorization.txt` — 鉴权与数据暴露模式定义
- `agents/prompts/patterns/intent.txt` — 需求意图与语义一致性模式定义
- `agents/prompts/patterns/lifecycle.txt` — 生命周期与状态一致性模式定义
- `util/pattern_detector.py` — Diff 模式检测器 + 模式文件加载器
- `test/test_progressive_context.py` — 18 个单元测试（9 模式检测 + 6 模式加载 + 2 prompt 组装 + 1 长度验证）

**修改文件**：
- `agents/nodes/intent_analysis.py` — 新增 `_assemble_intent_prompt()` 函数，替换原来的 `render_prompt_template("intent_analysis", ...)`
- `agents/nodes/intent_analysis_chunked.py` — 在 chunked 模式中也注入检测到的模式定义

### 核心逻辑

**动态模式检测**（`util/pattern_detector.py`）：
- `detect_patterns_from_diff(diff_content)`：对 diff 内容做关键词匹配（正则），返回检测到的模式列表
  - async/forEach/Promise → concurrency
  - .get()/null/try/catch → robustness
  - sql()/eval()/hasPermission/token → authorization
  - &&/|| 混用/start_date/feature flag → intent
  - useEffect/updateMany/subscribe → lifecycle
- 如果未检测到任何模式，默认返回所有模式（保守策略）
- `load_pattern_text(pattern_name)`：从 `agents/prompts/patterns/` 目录加载对应模式文件

**动态 prompt 组装**（`intent_analysis.py::_assemble_intent_prompt()`）：
1. 加载 `intent_core.txt` 作为基础（角色定义、输出格式、锚定规则）
2. 对当前文件的 diff 调用 `detect_patterns_from_diff()` 检测相关模式
3. 对每个检测到的模式，加载对应 `patterns/{pattern}.txt` 并追加到基础 prompt 之后
4. chunked 模式同理，在 `intent_analysis_chunked.py` 中预加载模式文本块

### 测试结果

- 18/18 单元测试通过
- 与 Step 1+2 合计 31/31 通过（1 集成测试跳过）
- workflow 导入验证通过
- 验证：仅含 async 变更的 diff，组装后的 prompt 不包含 lifecycle/intent/authorization 模式定义

### 后续开发者注意

- `intent_analysis.txt` 原始文件（200 行完整版）保留未删除，可作为参考
- pattern 关键词列表 `_PATTERN_KEYWORDS` 中的正则可以按需增减，不影响已有逻辑
- 如果新增风险类型，需同步在 `_PATTERN_KEYWORDS`、`_PATTERN_TO_FILE`、和 `patterns/` 目录中添加对应条目

---

## Step 4: 配置项与回退机制（已完成）

### 做了什么

**新增文件**：
- `test/test_harness_config.py` — 9 个单元测试（配置加载/条件边/回退）

**修改文件**：
- `core/config.py` — 在 `SystemConfig` 中添加 4 个 harness 配置字段
- `config.yaml` — 添加对应的 harness 配置项（默认全部启用）
- `agents/workflow.py` — 新增 `_cfg_bool()` 辅助函数，`create_multi_agent_workflow()` 根据配置条件性插入 verify_loop 和 eval_gate 节点和边
- `agents/nodes/intent_analysis.py` — 新增 `_should_use_progressive_context()` 和 `_assemble_legacy_prompt()`，根据配置选择动态或静态 prompt

### 核心逻辑

**配置项**（在 `config.yaml` 的 `system` 段下）：
- `harness_verify_loop_enabled: true` — 是否启用 verify_loop 节点
- `harness_verify_confidence_floor: 0.3` — verify_loop 对未锚定项的 confidence 下限
- `harness_eval_gate_enabled: true` — 是否启用 eval_gate 节点
- `harness_progressive_context_enabled: true` — 是否启用动态 prompt 组装

**回退机制**：
- 将任一 harness 配置设为 `false`，workflow 编译时不添加对应节点，边直接绕过
- `harness_progressive_context_enabled: false` 时，intent_analysis 回退到原始 200 行固定 prompt
- 所有 harness 默认启用，禁用后完全不影响原有行为（等价于改造前的 pipeline）

### 测试结果

- 9/9 单元测试通过
- 与 Step 1-3 合计 40/40 通过（1 集成测试跳过）
- 验证：禁用 verify_loop 和 eval_gate 时 workflow 能正常编译

---

## Step 5: 端到端验证与验收（已完成）

### 做了什么

**验证内容**：
1. **配置加载** — config.yaml 中 harness 字段能正确加载，`_cfg_bool()` 对启用/禁用场景返回正确值
2. **Workflow 编译** — 所有 harness 启用时 pipeline 包含 7 个节点（intent_router → intent_analysis → verify_loop → manager → expert_execution → eval_gate → reporter）；全部禁用时退化为 5 节点原始 pipeline
3. **动态 prompt 组装** — 含 async 变更的 diff 组装后 97 行，相比原始 209 行减少 54%；lifecycle 详细定义未被注入（仅核心指令中简略提及）
4. **模式检测准确率** — async/forEach/Promise 关键词正确触发 concurrency 模式；.get()/null/try/catch 触发 robustness 模式
5. **全量测试** — 40/40 单元测试通过（Step 1-4 全部），1 集成测试跳过（需 `RUN_INTEGRATION_TESTS=1`）

### 验证结果

| 验证项 | 预期 | 实际 | 状态 |
|---|---|---|---|
| harness 配置加载 | 4 个字段均存在 | 通过 | ✅ |
| 默认全部启用 | all True | 通过 | ✅ |
| 全部禁用可编译 | 5 节点 pipeline | 通过 | ✅ |
| 部分禁用 | 对应节点跳过 | 通过 | ✅ |
| progressive_context 回退 | legacy prompt 正常渲染 | 通过 | ✅ |
| prompt 长度缩减 | < 200 行 | 97 行（54%↓） | ✅ |
| 模式检测准确率 | 只检测相关模式 | 通过 | ✅ |
| 全量测试 | 40/40 | 40/40 | ✅ |

### 后续开发者注意

- 完整端到端测试（真实 LLM API + dataset 目录中的 git 仓库）需要 API key 和本地 git 仓库，不在代码实现范围内
- 生成 result_2.md 与 result_1.md 基线对比需要运行 `python main.py` 对 dataset/ 中的真实 PR 进行审查
- 所有 harness 功能的单元测试已覆盖核心逻辑，真实效果取决于 LLM 输出质量
