"""Eval Gate 节点的单元测试和集成测试。

测试策略：
1. 单元测试：构造含误报/真报的 expert_results，验证 disputed 项 confidence 被显著降低
2. 单元测试：确认 confirmed 项 confidence 被提升至 ≥0.7
3. 单元测试：uncertain 项 confidence 衰减为原值 0.8 倍
4. 集成测试：使用真实 PR diff 运行到 eval_gate 节点
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.state import ReviewState, RiskItem, RiskType
from agents.nodes.eval_gate import eval_gate_node


def _make_llm_mock(verdict: str, final_confidence: float, reason: str = "") -> AsyncMock:
    """构造 LLM mock，返回指定的验证结果。"""

    class MockResp:
        content = json.dumps({
            "verdict": verdict,
            "final_confidence": final_confidence,
            "reason": reason,
        })

    async def ainvoke(*args, **kwargs):
        return MockResp()

    mock_llm = AsyncMock()
    mock_llm.ainvoke = ainvoke
    return mock_llm


def _make_state_with_expert_results(
    risk_items: list[RiskItem],
    risk_type: str | None = None,
) -> ReviewState:
    """构造包含指定 expert_results 的 ReviewState。

    risk_type 默认取第一个 RiskItem 的 risk_type.value。
    """
    if risk_type is None and risk_items:
        risk_type = risk_items[0].risk_type.value
    elif risk_type is None:
        risk_type = "Robustness_Boundary_Conditions"
    return {
        "messages": [],
        "diff_context": "",
        "changed_files": ["src/test/file.py"],
        "file_analyses": [],
        "work_list": [],
        "expert_tasks": {},
        "expert_results": {
            risk_type: [item.model_dump() for item in risk_items],
        },
        "confirmed_issues": [],
        "final_report": "",
        "lint_errors": [],
        "metadata": {
            "llm": None,
            "config": MagicMock(system=MagicMock(
                max_concurrent_llm_requests=2,
            )),
            "workflow_version": "test",
            "config_provider": "test",
            "confidence_threshold": 0.6,
            "run_started_at": 0.0,
        },
    }


class TestEvalGateUnit:
    """单元测试：Eval Gate 节点的正确性。"""

    def test_disputed_risk_confidence_lowered(self):
        """被 disputed 的风险项 confidence 应降至 0.3。

        模拟场景：专家说"Prisma 非空字段可能为 null"，但实际 schema 中该字段非空。
        """
        risk = RiskItem(
            risk_type=RiskType.ROBUSTNESS_BOUNDARY_CONDITIONS,
            file_path="src/test/file.py",
            line_number=(10, 10),
            description="credential.type 可能为 null",
            confidence=0.8,
            severity="error",
        )
        state = _make_state_with_expert_results([risk])

        async def run():
            state["metadata"]["llm"] = _make_llm_mock(
                verdict="disputed",
                final_confidence=0.3,
                reason="schema 中 type 是 String @nonNull，不会为 null",
            )
            with patch("agents.nodes.eval_gate.read_file_content", return_value="line 10: type String\n"):
                with patch("agents.nodes.eval_gate.extract_file_diff", return_value=""):
                    return await eval_gate_node(state)

        output = asyncio.run(run())
        results = output["expert_results"]
        assert "Robustness_Boundary_Conditions" in results
        assert len(results["Robustness_Boundary_Conditions"]) == 1
        assert results["Robustness_Boundary_Conditions"][0]["confidence"] == 0.3

    def test_confirmed_risk_confidence_raised(self):
        """被 confirmed 的风险项 confidence 应提升至 ≥0.7。"""
        risk = RiskItem(
            risk_type=RiskType.AUTHORIZATION_DATA_EXPOSURE,
            file_path="src/test/file.py",
            line_number=(20, 20),
            description="request.GET['installation_id'] 缺异常保护",
            confidence=0.5,
            severity="warning",
        )
        state = _make_state_with_expert_results([risk])

        async def run():
            state["metadata"]["llm"] = _make_llm_mock(
                verdict="confirmed",
                final_confidence=0.7,
                reason="确实没有 try-catch 或 .get() 保护",
            )
            with patch("agents.nodes.eval_gate.read_file_content", return_value="line 20: x = request.GET['id']\n"):
                with patch("agents.nodes.eval_gate.extract_file_diff", return_value=""):
                    return await eval_gate_node(state)

        output = asyncio.run(run())
        results = output["expert_results"]
        assert results["Authorization_Data_Exposure"][0]["confidence"] >= 0.7

    def test_uncertain_risk_decayed(self):
        """被 uncertain 的风险项 confidence 应衰减为原值的 0.8 倍。"""
        risk = RiskItem(
            risk_type=RiskType.CONCURRENCY_TIMING_CORRECTNESS,
            file_path="src/test/file.py",
            line_number=(30, 30),
            description="可能存在并发竞态",
            confidence=0.5,
            severity="info",
        )
        state = _make_state_with_expert_results([risk])

        async def run():
            state["metadata"]["llm"] = _make_llm_mock(
                verdict="uncertain",
                final_confidence=0.4,  # 0.5 * 0.8
                reason="没有找到锁，但也没有直接证据证明会并发执行",
            )
            with patch("agents.nodes.eval_gate.read_file_content", return_value="line 30: pass\n"):
                with patch("agents.nodes.eval_gate.extract_file_diff", return_value=""):
                    return await eval_gate_node(state)

        output = asyncio.run(run())
        results = output["expert_results"]
        assert results["Concurrency_Timing_Correctness"][0]["confidence"] == pytest.approx(0.4, abs=0.01)

    def test_mixed_verdicts(self):
        """混合场景：同时有 confirmed/disputed/uncertain，应分别处理。"""
        risks = [
            RiskItem(
                risk_type=RiskType.ROBUSTNESS_BOUNDARY_CONDITIONS,
                file_path="src/test/file.py",
                line_number=(5, 5),
                description="obj.get 可能返回 None",
                confidence=0.6,
            ),
            RiskItem(
                risk_type=RiskType.ROBUSTNESS_BOUNDARY_CONDITIONS,
                file_path="src/test/file.py",
                line_number=(10, 10),
                description="字段类型不可能为 null",
                confidence=0.8,
            ),
            RiskItem(
                risk_type=RiskType.ROBUSTNESS_BOUNDARY_CONDITIONS,
                file_path="src/test/file.py",
                line_number=(15, 15),
                description="可能存在边界问题",
                confidence=0.5,
            ),
        ]
        state = _make_state_with_expert_results(risks)

        responses = [
            {"verdict": "confirmed", "final_confidence": 0.7, "reason": "确无判空"},
            {"verdict": "disputed", "final_confidence": 0.3, "reason": "schema 非空"},
            {"verdict": "uncertain", "final_confidence": 0.4, "reason": "证据不足"},
        ]

        async def run():
            nonlocal responses
            call_idx = 0

            async def ainvoke(*a, **kw):
                nonlocal call_idx
                class R:
                    content = json.dumps(responses[call_idx])
                call_idx += 1
                return R

            state["metadata"]["llm"] = AsyncMock(ainvoke=ainvoke)
            with patch("agents.nodes.eval_gate.read_file_content", return_value="dummy"):
                with patch("agents.nodes.eval_gate.extract_file_diff", return_value=""):
                    return await eval_gate_node(state)

        output = asyncio.run(run())
        items = output["expert_results"]["Robustness_Boundary_Conditions"]
        # confirmed: max(0.6, 0.7) = 0.7
        assert items[0]["confidence"] >= 0.7
        # disputed: 0.3
        assert items[1]["confidence"] == 0.3
        # uncertain: 0.5 * 0.8 = 0.4
        assert items[2]["confidence"] == pytest.approx(0.4, abs=0.01)

    def test_empty_expert_results_skips(self):
        """空 expert_results 应直接跳过。"""
        state: ReviewState = {
            "messages": [],
            "diff_context": "",
            "changed_files": [],
            "file_analyses": [],
            "work_list": [],
            "expert_tasks": {},
            "expert_results": {},
            "confirmed_issues": [],
            "final_report": "",
            "lint_errors": [],
            "metadata": {
                "llm": AsyncMock(),  # 即使有 LLM 也不应被调用
                "config": MagicMock(system=MagicMock(max_concurrent_llm_requests=2)),
                "workflow_version": "test",
                "config_provider": "test",
                "confidence_threshold": 0.6,
                "run_started_at": 0.0,
            },
        }

        async def run():
            return await eval_gate_node(state)

        output = asyncio.run(run())
        assert output["expert_results"] == {}

    def test_llm_error_keeps_risk_with_slight_penalty(self):
        """LLM 调用失败时，风险项应保留但 confidence 轻微降低。"""
        risk = RiskItem(
            risk_type=RiskType.ROBUSTNESS_BOUNDARY_CONDITIONS,
            file_path="src/test/file.py",
            line_number=(10, 10),
            description="test risk",
            confidence=0.7,
        )
        state = _make_state_with_expert_results([risk])

        async def run():
            failing_llm = AsyncMock(ainvoke=AsyncMock(side_effect=Exception("LLM error")))
            state["metadata"]["llm"] = failing_llm
            with patch("agents.nodes.eval_gate.read_file_content", return_value="content"):
                with patch("agents.nodes.eval_gate.extract_file_diff", return_value=""):
                    return await eval_gate_node(state)

        output = asyncio.run(run())
        items = output["expert_results"]["Robustness_Boundary_Conditions"]
        assert len(items) == 1
        # confidence 应轻微降低（原值 * 0.95），但不低于 0.3
        assert 0.3 <= items[0]["confidence"] < 0.7


class TestEvalGateEdgeCases:
    """边界情况测试。"""

    def test_confidence_clamped_to_valid_range(self):
        """confidence 应被限制在 [0.0, 1.0] 范围内。"""
        # confirmed 场景：专家 confidence 为 0.9，final 应为 max(0.9, 0.7) = 0.9
        risk = RiskItem(
            risk_type=RiskType.ROBUSTNESS_BOUNDARY_CONDITIONS,
            file_path="src/test/file.py",
            line_number=(1, 1),
            description="test",
            confidence=0.9,
        )
        state = _make_state_with_expert_results([risk])

        async def run():
            state["metadata"]["llm"] = _make_llm_mock(
                verdict="confirmed",
                final_confidence=0.9,
                reason="confirmed",
            )
            with patch("agents.nodes.eval_gate.read_file_content", return_value="x"):
                with patch("agents.nodes.eval_gate.extract_file_diff", return_value=""):
                    return await eval_gate_node(state)

        output = asyncio.run(run())
        items = output["expert_results"]["Robustness_Boundary_Conditions"]
        assert 0.0 <= items[0]["confidence"] <= 1.0

    def test_multiple_risk_types_handled(self):
        """多个风险类型的结果应分别处理。"""
        risks_robustness = [
            RiskItem(
                risk_type=RiskType.ROBUSTNESS_BOUNDARY_CONDITIONS,
                file_path="src/test/file.py",
                line_number=(1, 1),
                description="null risk",
                confidence=0.6,
            ),
        ]
        risks_concurrency = [
            RiskItem(
                risk_type=RiskType.CONCURRENCY_TIMING_CORRECTNESS,
                file_path="src/test/other.py",
                line_number=(2, 2),
                description="race condition",
                confidence=0.7,
            ),
        ]
        state = _make_state_with_expert_results(risks_robustness, "Robustness_Boundary_Conditions")
        # Add second risk type
        state["expert_results"]["Concurrency_Timing_Correctness"] = [
            item.model_dump() for item in risks_concurrency
        ]

        call_idx = 0
        responses = [
            {"verdict": "disputed", "final_confidence": 0.3, "reason": "has guard"},
            {"verdict": "confirmed", "final_confidence": 0.7, "reason": "no lock found"},
        ]

        async def run():
            nonlocal call_idx

            async def ainvoke(*a, **kw):
                nonlocal call_idx
                class R:
                    content = json.dumps(responses[call_idx])
                call_idx += 1
                return R

            state["metadata"]["llm"] = AsyncMock(ainvoke=ainvoke)
            with patch("agents.nodes.eval_gate.read_file_content", return_value="x"):
                with patch("agents.nodes.eval_gate.extract_file_diff", return_value=""):
                    return await eval_gate_node(state)

        output = asyncio.run(run())
        assert "Robustness_Boundary_Conditions" in output["expert_results"]
        assert "Concurrency_Timing_Correctness" in output["expert_results"]
        assert len(output["expert_results"]["Robustness_Boundary_Conditions"]) == 1
        assert len(output["expert_results"]["Concurrency_Timing_Correctness"]) == 1
        # robustness → disputed → 0.3
        assert output["expert_results"]["Robustness_Boundary_Conditions"][0]["confidence"] == 0.3
        # concurrency → confirmed → max(0.7, 0.7) = 0.7
        assert output["expert_results"]["Concurrency_Timing_Correctness"][0]["confidence"] >= 0.7


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
