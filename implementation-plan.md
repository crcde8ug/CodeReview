# 三项改进实施计划

## Context

当前 CodeReview 的 LangGraph 流水线为：
`intent_router → intent_analysis → manager → expert_execution → reporter`

存在三个可改进点：
1. 专家输出缺乏确定性验证，依赖模型自评（偏乐观）→ 误报率高
2. 意图分析仅逐文件独立分析，无文件间依赖关系检测 → 漏报率偏高
3. 无评估工具衡量 Precision/Recall/F1 → 调参无据可依

---

## 改动 1：验证节点（Verifier Node）

### 目标
在 `expert_execution` 和 `reporter` 之间插入确定性验证节点，用 CPG 工具校验专家结论，不调用 LLM。

### 新增文件
**`agents/nodes/verifier.py`**

```python
async def verifier_node(state: ReviewState) -> Dict[str, Any]:
```

四条验证检查：

| 检查 | 适用类型 | 逻辑 | 置信度调整 |
|---|---|---|---|
| **行号锚定** | 全部 | 复用 `manager._is_anchored_to_changes`，检查行号是否在 diff ± window 内 | 未锚定: -0.3 |
| **符号存在** | 描述含函数/类名的 | 直接调用 `cpg_symbol_search` 验证符号是否在声称位置存在 | 不存在: -0.3 |
| **防御代码** | Robustness 类 | 在风险行号 ±10 行 grep 判空/try-catch/安全导航 | 找到防御: -0.3 |
| **调用关系** | 描述含跨文件调用的 | `cpg_callgraph` 验证声称的调用链是否存在 | 证实+0.1, 反驳-0.3 |

- CPG 不可用时仅运行行号锚定检查（优雅降级）
- 验证结果存为 dict 中的 `verification_notes` 键（无需改 Pydantic schema）

### 修改文件
- **`agents/workflow.py`**: 添加 verifier 节点，条件边 `expert_execution → verifier → reporter`
- **`core/config.py`**: 新增 `enable_verifier`, `verifier_anchor_window`, `verifier_guard_window`

---

## 改动 2：文件间意图分析节点（Cross-File Intent Node）

### 目标
在 `intent_analysis` 之后、`manager` 之前，构建变更文件间的导入/调用依赖图，识别跨文件风险。

### 新增文件
**`agents/nodes/cross_file_intent.py`**

```python
async def cross_file_intent_node(state: ReviewState) -> Dict[str, Any]:
```

逻辑：
1. 调用 `ast_index()` 获取所有变更文件的 imports/calls/defs（head 版本）
2. 构建依赖图：file_a → file_b 当 a 导入/调用 b 中定义的符号
3. 对两个都变更的文件对 (A, B)，检测：
   - A 新增了对 B 的调用/导入（看 A 的 diff 行）
   - B 的公开 API 是否变化（对比 head vs base 的 defs）
4. 生成 `RiskItem` 附加到 `file_analyses`（用 `file_path="__cross_file__"` 的 synthetic FileAnalysis）
5. 单次 CPG 查询 + 无 LLM 调用，轻量级

### 修改文件
- **`agents/workflow.py`**: 添加 cross_file_intent 节点，边改为 `intent_analysis → cross_file_intent → manager`
- **`core/config.py`**: 新增 `enable_cross_file_intent`

---

## 改动 3：评估工具（Evaluation Harness）

### 目标
对 test_cases.json 的 29 个测试用例跑审查，计算 Precision/Recall/F1。

### 新增文件
**`test/eval_utils.py`** — 共享工具（加载用例、过滤、解析）
**`test/eval_harness.py`** — 主入口

```
python test/eval_harness.py --repos sentry,cal.com --cases 1-10
python test/eval_harness.py --all
```

匹配策略：
- **Expected Bug 提取**: case name 格式 `PR描述_Bug描述`，取最后一段作为预期 bug
- **Review Finding 解析**: 从 markdown 报告用正则提取 file/line/risk_type/description
- **匹配**: 文件路径子串匹配 + 描述关键词重叠 ≥ 2 个
- **输出**: JSON + CSV 报告，含 per-case 和 aggregate 指标

### 修改文件
- **`core/state.py`**: `RiskItem` 加 `model_config = ConfigDict(extra='allow')` 支持 `verification_notes`

---

## 实施顺序

1. **改动 3**（评估工具）— 先有测量能力，再优化才有据可依
2. **改动 1**（验证节点）— 纯确定性逻辑，不依赖 LLM，风险最低
3. **改动 2**（文件间分析）— 最复杂，依赖 CPG 可用性

## 关键文件路径

| 文件 | 操作 | 原因 |
|---|---|---|
| `agents/nodes/verifier.py` | 新增 | 验证逻辑 |
| `agents/nodes/cross_file_intent.py` | 新增 | 跨文件分析 |
| `test/eval_utils.py` | 新增 | 测试共享工具 |
| `test/eval_harness.py` | 新增 | 评估主入口 |
| `agents/workflow.py` | 修改 | 接入新节点 |
| `core/config.py` | 修改 | 新增开关配置 |
| `core/state.py` | 修改 | RiskItem 支持 extra 字段 |

## 验证方式

1. 实施改动 3 后，先跑 baseline：`python test/eval_harness.py --all` 得到当前 P/R/F1
2. 接入改动 1，再跑评估，对比 FP 是否下降
3. 接入改动 2，再跑评估，对比 FN 是否下降
4. 每次对比用 git diff 确认配置变化正确
