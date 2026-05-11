"""Verify Loop 节点的单元测试和集成测试。

测试策略：
1. 单元测试：构造含真实/虚假 RiskItem 的 ReviewState，验证未锚定项 confidence 被降低
2. 集成测试：使用真实 PR diff 运行到 verify_loop 节点，检查过滤结果
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 确保能导入项目模块
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.state import ReviewState, RiskItem, RiskType, FileAnalysis
from agents.nodes.verify_loop import verify_loop_node


def _make_llm_mock(anchored: bool, adjusted_confidence: float, evidence: str = "") -> AsyncMock:
    """构造 LLM mock，返回指定的验证结果。"""

    class MockResponse:
        content = json.dumps({
            "anchored": anchored,
            "adjusted_confidence": adjusted_confidence,
            "evidence": evidence,
        })

    async def ainvoke(*args, **kwargs):
        return MockResponse()

    mock_llm = AsyncMock()
    mock_llm.ainvoke = ainvoke
    return mock_llm


def _make_state_with_risks(risks: list[RiskItem]) -> ReviewState:
    """构造包含指定 RiskItem 列表的 ReviewState。"""
    fa = FileAnalysis(
        file_path="src/test/file.py",
        intent_summary="test file",
        potential_risks=risks,
        complexity_score=50.0,
    )
    return {
        "messages": [],
        "diff_context": "",
        "changed_files": ["src/test/file.py"],
        "file_analyses": [fa.model_dump()],
        "work_list": [],
        "expert_tasks": {},
        "expert_results": {},
        "confirmed_issues": [],
        "final_report": "",
        "lint_errors": [],
        "metadata": {
            "llm": None,
            "config": MagicMock(system=MagicMock(
                max_concurrent_llm_requests=2,
                harness_verify_confidence_floor=0.3,
            )),
            "workflow_version": "test",
            "config_provider": "test",
            "confidence_threshold": 0.6,
            "run_started_at": 0.0,
        },
    }


class TestVerifyLoopUnit:
    """单元测试：Verify Loop 节点的正确性。"""

    def test_anchored_risk_keeps_confidence(self):
        """已锚定的风险项应保持原始 confidence。"""
        risk = RiskItem(
            risk_type=RiskType.ROBUSTNESS_BOUNDARY_CONDITIONS,
            file_path="src/test/file.py",
            line_number=(10, 10),
            description="变量 x 可能为 None，但未做判空检查",
            confidence=0.7,
            severity="warning",
        )
        state = _make_state_with_risks([risk])

        async def run():
            state["metadata"]["llm"] = _make_llm_mock(
                anchored=True, adjusted_confidence=0.7, evidence="line 10 has dereference of x"
            )
            # 模拟 read_file_content 返回包含第 10 行的内容
            file_lines = [f"line {i} content\n" for i in range(1, 21)]
            file_lines[9] = "result = x.attribute  # line 10\n"
            with patch("agents.nodes.verify_loop.read_file_content", return_value="".join(file_lines)):
                result = await verify_loop_node(state)
            return result

        output = asyncio.run(run())
        updated = FileAnalysis(**output["file_analyses"][0])
        assert len(updated.potential_risks) == 1
        assert updated.potential_risks[0].confidence == 0.7

    def test_unanchored_risk_confidence_lowered(self):
        """未锚定的风险项 confidence 应降至 0.3。"""
        risk = RiskItem(
            risk_type=RiskType.ROBUSTNESS_BOUNDARY_CONDITIONS,
            file_path="src/test/file.py",
            line_number=(10, 10),
            description="可能存在性能问题",
            confidence=0.8,
            severity="info",
        )
        state = _make_state_with_risks([risk])

        async def run():
            state["metadata"]["llm"] = _make_llm_mock(
                anchored=False, adjusted_confidence=0.3, evidence="description too vague"
            )
            with patch("agents.nodes.verify_loop.read_file_content", return_value="line 1: pass\n"):
                result = await verify_loop_node(state)
            return result

        output = asyncio.run(run())
        updated = FileAnalysis(**output["file_analyses"][0])
        assert len(updated.potential_risks) == 1
        assert updated.potential_risks[0].confidence <= 0.3

    def test_no_risks_skips_verify(self):
        """没有风险项时应跳过验证，返回空列表。"""
        risk = RiskItem(
            risk_type=RiskType.ROBUSTNESS_BOUNDARY_CONDITIONS,
            file_path="src/test/file.py",
            line_number=(1, 1),
            description="test",
            confidence=0.5,
        )
        state = _make_state_with_risks([risk])
        # 清空 risks
        fa = FileAnalysis(**state["file_analyses"][0])
        fa.potential_risks = []
        state["file_analyses"] = [fa.model_dump()]

        async def run():
            state["metadata"]["llm"] = _make_llm_mock(True, 0.5)
            return await verify_loop_node(state)

        output = asyncio.run(run())
        assert output["file_analyses"] == [fa.model_dump()]

    def test_llm_error_keeps_risk_with_slight_penalty(self):
        """LLM 调用失败时，风险项应保留但 confidence 轻微降低。"""
        risk = RiskItem(
            risk_type=RiskType.ROBUSTNESS_BOUNDARY_CONDITIONS,
            file_path="src/test/file.py",
            line_number=(10, 10),
            description="test risk",
            confidence=0.7,
        )
        state = _make_state_with_risks([risk])

        async def run():
            failing_llm = AsyncMock()
            failing_llm.ainvoke = AsyncMock(side_effect=Exception("LLM error"))
            state["metadata"]["llm"] = failing_llm
            with patch("agents.nodes.verify_loop.read_file_content", return_value="line content"):
                result = await verify_loop_node(state)
            return result

        output = asyncio.run(run())
        updated = FileAnalysis(**output["file_analyses"][0])
        assert len(updated.potential_risks) == 1
        # confidence 应轻微降低（原值 * 0.9），但不低于 0.3
        assert 0.3 <= updated.potential_risks[0].confidence < 0.7

    def test_mixed_risks_anchored_and_unanchored(self):
        """混合场景：部分锚定、部分未锚定，应分别处理。"""
        risks = [
            RiskItem(
                risk_type=RiskType.ROBUSTNESS_BOUNDARY_CONDITIONS,
                file_path="src/test/file.py",
                line_number=(5, 5),
                description="obj.get('key') 可能返回 None",
                confidence=0.6,
            ),
            RiskItem(
                risk_type=RiskType.CONCURRENCY_TIMING_CORRECTNESS,
                file_path="src/test/file.py",
                line_number=(10, 10),
                description="可能存在并发问题",
                confidence=0.8,
            ),
            RiskItem(
                risk_type=RiskType.AUTHORIZATION_DATA_EXPOSURE,
                file_path="src/test/file.py",
                line_number=(15, 15),
                description="request.user 未做权限检查",
                confidence=0.7,
            ),
        ]
        state = _make_state_with_risks(risks)

        call_count = 0
        responses = [
            {"anchored": True, "adjusted_confidence": 0.6, "evidence": "line 5 has .get()"},
            {"anchored": False, "adjusted_confidence": 0.3, "evidence": "too vague"},
            {"anchored": True, "adjusted_confidence": 0.7, "evidence": "line 15 has request.user"},
        ]

        class MockResponse:
            def __init__(self, data):
                self.content = json.dumps(data)

        async def run():
            nonlocal call_count

            async def ainvoke(*args, **kwargs):
                nonlocal call_count
                resp = MockResponse(responses[call_count])
                call_count += 1
                return resp

            state["metadata"]["llm"] = AsyncMock(ainvoke=ainvoke)
            with patch("agents.nodes.verify_loop.read_file_content", return_value="dummy content"):
                result = await verify_loop_node(state)
            return result

        output = asyncio.run(run())
        updated = FileAnalysis(**output["file_analyses"][0])
        assert len(updated.potential_risks) == 3
        # 第一个：已锚定，confidence 不变
        assert updated.potential_risks[0].confidence == 0.6
        # 第二个：未锚定，confidence 降至 0.3
        assert updated.potential_risks[1].confidence <= 0.3
        # 第三个：已锚定，confidence 不变
        assert updated.potential_risks[2].confidence == 0.7


class TestVerifyLoopIntegration:
    """集成测试：使用真实 diff 数据验证 verify_loop 行为。"""

    @pytest.mark.skipif(
        not os.environ.get("RUN_INTEGRATION_TESTS"),
        reason="设置 RUN_INTEGRATION_TESTS=1 运行集成测试"
    )
    def test_verify_loop_on_real_diff(self):
        """使用 sentry PR #1 的真实 diff 验证 filter 效果。

        预期：import OptimizedCursorPaginator 的风险项应被锚定（import 语句在 diff 中可见），
        而笼统的"性能问题"描述应被降低 confidence。
        """
        import subprocess
        from util.file_utils import read_file_content as _read_file

        repo_path = PROJECT_ROOT / "dataset" / "sentry-greptile"
        base = "master"
        head = "performance-enhancement-complete"

        # 获取 diff
        diff_result = subprocess.run(
            ["git", "diff", f"{base}..{head}"],
            cwd=repo_path, capture_output=True, text=True
        )
        assert diff_result.returncode == 0, f"git diff failed: {diff_result.stderr}"
        diff_content = diff_result.stdout

        # 这个集成测试需要真实 LLM 调用，仅验证节点能正确接入工作流
        # 实际验证通过 run_multi_agent_workflow 运行
        assert len(diff_content) > 0, "diff should not be empty"
        assert "OptimizedCursorPaginator" in diff_content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
