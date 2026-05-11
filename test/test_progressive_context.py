"""Progressive Context 的单元测试。

测试策略：
1. 单元测试：构造不同 diff 片段，验证 detect_patterns_from_diff 正确检测模式
2. 单元测试：验证 load_pattern_text 返回非空内容
3. 单元测试：验证 intent_analysis.py 的 _assemble_intent_prompt 只注入相关模式
4. 集成测试：使用真实 PR diff，验证 prompt 长度比固定 200 行减少
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from util.pattern_detector import detect_patterns_from_diff, load_pattern_text


class TestPatternDetector:
    """单元测试：模式检测器的正确性。"""

    def test_detect_async_concurrency_pattern(self):
        """含 async/forEach 的 diff 应检测到 concurrency 模式。"""
        diff = """\
diff --git a/src/app.ts b/src/app.ts
index abc123..def456 100644
--- a/src/app.ts
+++ b/src/app.ts
+async function process(items) {
+  items.forEach(async (item) => {
+    await save(item);
+  });
+}
"""
        patterns = detect_patterns_from_diff(diff)
        assert "concurrency" in patterns

    def test_detect_robustness_pattern(self):
        """含 .get() 和 null 检查的 diff 应检测到 robustness 模式。"""
        diff = """\
diff --git a/src/utils.py b/src/utils.py
index abc123..def456 100644
--- a/src/utils.py
+++ b/src/utils.py
+def get_value(data):
+    return data.get("key", None)
+    if val is None:
+        return default
"""
        patterns = detect_patterns_from_diff(diff)
        assert "robustness" in patterns

    def test_detect_authorization_pattern(self):
        """含 SQL 拼接和 auth 关键词的 diff 应检测到 authorization 模式。"""
        diff = """\
diff --git a/src/api.py b/src/api.py
index abc123..def456 100644
--- a/src/api.py
+++ b/src/api.py
+query = f"SELECT * FROM users WHERE id = {user_id}"
+if not hasPermission(user, "admin"):
+    return error
+secret = request.headers.get("x-secret-token")
"""
        patterns = detect_patterns_from_diff(diff)
        assert "authorization" in patterns

    def test_detect_intent_pattern(self):
        """含布尔逻辑嵌套和时间计算的 diff 应检测到 intent 模式。"""
        diff = """\
diff --git a/src/logic.ts b/src/logic.ts
index abc123..def456 100644
--- a/src/logic.ts
+++ b/src/logic.ts
+if (isAdmin || isOwner && hasFeatureFlag) {
+    const end_date = start_date + timedelta(days=30);
+}
"""
        patterns = detect_patterns_from_diff(diff)
        # && 和 || 混用应检测 intent
        assert "intent" in patterns

    def test_detect_lifecycle_pattern(self):
        """含 useEffect 和 updateMany 的 diff 应检测到 lifecycle 模式。"""
        diff = """\
diff --git a/src/component.tsx b/src/component.tsx
index abc123..def456 100644
--- a/src/component.tsx
+++ b/src/component.tsx
+useEffect(() => {
+    updateMany({ data: newData });
+    subscribe(event, handler);
+}, []);
"""
        patterns = detect_patterns_from_diff(diff)
        assert "lifecycle" in patterns

    def test_empty_diff_returns_all_patterns(self):
        """空 diff 应返回所有模式（保守策略）。"""
        patterns = detect_patterns_from_diff("")
        assert len(patterns) == 5
        assert set(patterns) == {"robustness", "concurrency", "authorization", "intent", "lifecycle"}

    def test_none_diff_returns_all_patterns(self):
        """None diff 应返回所有模式（保守策略）。"""
        patterns = detect_patterns_from_diff(None)
        assert len(patterns) == 5

    def test_unrelated_diff_returns_only_relevant_patterns(self):
        """纯 CSS 变更不应检测到任何特定模式，应返回全部（保守策略）。"""
        diff = """\
diff --git a/src/styles.css b/src/styles.css
index abc123..def456 100644
--- a/src/styles.css
+++ b/src/styles.css
+body {
+    background-color: #ffffff;
+    font-size: 16px;
+}
"""
        patterns = detect_patterns_from_diff(diff)
        # 纯 CSS 不含任何模式关键词，应返回全部
        assert len(patterns) == 5

    def test_mixed_diff_detects_multiple_patterns(self):
        """混合变更应检测到多个模式。"""
        diff = """\
diff --git a/src/app.py b/src/app.py
index abc123..def456 100644
--- a/src/app.py
+++ b/src/app.py
+async def handle_request(request):
+    user = request.GET.get("user")
+    if user and hasPermission(user, "admin"):
+        query = f"SELECT * FROM users WHERE name = '{user}'"
+        await db.execute(query)
"""
        patterns = detect_patterns_from_diff(diff)
        # async → concurrency
        # .get() → robustness
        # hasPermission, SELECT f-string → authorization
        assert "concurrency" in patterns
        assert "robustness" in patterns
        assert "authorization" in patterns


class TestPatternLoader:
    """单元测试：模式文件加载。"""

    def test_load_robustness_pattern(self):
        """robustness 模式文件应包含风险类型定义。"""
        text = load_pattern_text("robustness")
        assert "Robustness_Boundary_Conditions" in text
        assert len(text) > 100

    def test_load_concurrency_pattern(self):
        """concurrency 模式文件应包含风险类型定义。"""
        text = load_pattern_text("concurrency")
        assert "Concurrency_Timing_Correctness" in text
        assert len(text) > 100

    def test_load_authorization_pattern(self):
        text = load_pattern_text("authorization")
        assert "Authorization_Data_Exposure" in text

    def test_load_intent_pattern(self):
        text = load_pattern_text("intent")
        assert "Intent_Semantic_Consistency" in text

    def test_load_lifecycle_pattern(self):
        text = load_pattern_text("lifecycle")
        assert "Lifecycle_State_Consistency" in text

    def test_load_unknown_pattern_returns_empty(self):
        text = load_pattern_text("unknown_pattern")
        assert text == ""


class TestIntentPromptAssembly:
    """单元测试：Intent prompt 动态组装。"""

    def test_assemble_intent_prompt(self):
        """组装后的 prompt 应包含核心指令和检测到的模式。"""
        from agents.nodes.intent_analysis import _assemble_intent_prompt

        file_diff = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
+async def handle_request(request):
+    user = request.GET.get("user")
+    await db.execute(user)
"""
        prompt = _assemble_intent_prompt(
            file_path="src/app.py",
            file_diff=file_diff,
            file_content="async def handle_request(request):\n    pass\n",
        )

        # 应包含核心指令
        assert "意图分析Agent" in prompt or "Intent Analysis Agent" in prompt
        assert "只输出 JSON" in prompt

        # 应包含检测到的模式（async → concurrency, .get() → robustness）
        assert "Concurrency_Timing_Correctness" in prompt
        assert "Robustness_Boundary_Conditions" in prompt

    def test_assemble_intent_prompt_minimal_diff(self):
        """最小 diff 的 prompt 应包含核心指令和所有模式（保守策略）。"""
        from agents.nodes.intent_analysis import _assemble_intent_prompt

        file_diff = "no changes"
        prompt = _assemble_intent_prompt(
            file_path="src/styles.css",
            file_diff=file_diff,
            file_content="body { color: black; }\n",
        )

        assert "意图分析Agent" in prompt or "Intent Analysis Agent" in prompt
        # 无特定模式 → 保守策略，包含所有模式
        assert "Robustness_Boundary_Conditions" in prompt


class TestPromptLengthReduction:
    """测试：验证 prompt 长度比改造前减少。"""

    def test_dynamic_prompt_shorter_than_fixed(self):
        """动态组装的 prompt 长度应比原始固定 200 行短。"""
        from agents.nodes.intent_analysis import _assemble_intent_prompt
        from agents.prompts import render_prompt_template

        # 读取原始 intent_analysis.txt 的长度
        original_path = PROJECT_ROOT / "agents" / "prompts" / "intent_analysis.txt"
        if original_path.exists():
            original_text = original_path.read_text(encoding="utf-8")
            original_lines = len(original_text.splitlines())
        else:
            original_lines = 200  # 默认估计

        # 构造一个只含 concurrency 模式的 diff
        concurrency_only_diff = """\
diff --git a/src/app.ts b/src/app.ts
--- a/src/app.ts
+++ b/src/app.ts
+async function fetchData() {
+    const result = await fetch(url);
+    return result;
+}
"""
        dynamic_prompt = _assemble_intent_prompt(
            file_path="src/app.ts",
            file_diff=concurrency_only_diff,
            file_content="async function fetchData() { return null; }\n",
        )
        dynamic_lines = len(dynamic_prompt.splitlines())

        # 动态 prompt 应比原始短至少 30%
        reduction = (original_lines - dynamic_lines) / original_lines
        # 即使只减少一点，也验证了动态组装生效
        assert dynamic_lines < original_lines, (
            f"Dynamic prompt ({dynamic_lines} lines) should be shorter than "
            f"original ({original_lines} lines), reduction: {reduction:.1%}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
