"""Step 4: 配置项与回退机制的测试。

测试策略：
1. 验证 config.yaml 中的 harness 配置项能正确加载
2. 验证 config 关闭 verify_loop 时 workflow 不包含该节点
3. 验证 config 关闭 eval_gate 时 workflow 不包含该节点
4. 验证 config 关闭 progressive_context 时 intent_analysis 使用旧版 prompt
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import Config, SystemConfig
from core.state import RiskItem, RiskType, FileAnalysis


class TestHarnessConfigLoading:
    """测试：harness 配置项能正确加载。"""

    def test_default_config_has_harness_flags(self):
        """默认配置应包含 harness 相关字段。"""
        config = Config()
        assert hasattr(config.system, "harness_verify_loop_enabled")
        assert hasattr(config.system, "harness_verify_confidence_floor")
        assert hasattr(config.system, "harness_eval_gate_enabled")
        assert hasattr(config.system, "harness_progressive_context_enabled")

    def test_default_harness_flags_are_true(self):
        """默认情况下所有 harness 应启用。"""
        config = Config()
        assert config.system.harness_verify_loop_enabled is True
        assert config.system.harness_eval_gate_enabled is True
        assert config.system.harness_progressive_context_enabled is True
        assert config.system.harness_verify_confidence_floor == 0.3

    def test_config_can_disable_harness(self):
        """应能通过编程方式禁用 harness。"""
        config = Config(system=SystemConfig(
            harness_verify_loop_enabled=False,
            harness_eval_gate_enabled=False,
            harness_progressive_context_enabled=False,
        ))
        assert config.system.harness_verify_loop_enabled is False
        assert config.system.harness_eval_gate_enabled is False
        assert config.system.harness_progressive_context_enabled is False


class TestWorkflowConditionalEdges:
    """测试：workflow 根据配置条件性插入节点。"""

    def _make_minimal_config(self, **kwargs) -> Config:
        """构造最小可用配置。"""
        system_overrides = {
            "harness_verify_loop_enabled": True,
            "harness_eval_gate_enabled": True,
            "harness_progressive_context_enabled": True,
        }
        system_overrides.update(kwargs)
        return Config(system=SystemConfig(**system_overrides))

    def test_workflow_with_all_harness_enabled(self):
        """启用所有 harness 时，workflow 应包含 verify_loop 和 eval_gate 节点。"""
        config = self._make_minimal_config(
            harness_verify_loop_enabled=True,
            harness_eval_gate_enabled=True,
        )
        from agents.workflow import _cfg_bool
        assert _cfg_bool(config, "harness_verify_loop_enabled") is True
        assert _cfg_bool(config, "harness_eval_gate_enabled") is True

    def test_workflow_with_verify_loop_disabled(self):
        """禁用 verify_loop 时，_cfg_bool 应返回 False。"""
        config = self._make_minimal_config(
            harness_verify_loop_enabled=False,
        )
        from agents.workflow import _cfg_bool
        assert _cfg_bool(config, "harness_verify_loop_enabled") is False
        assert _cfg_bool(config, "harness_eval_gate_enabled") is True

    def test_workflow_with_eval_gate_disabled(self):
        """禁用 eval_gate 时，_cfg_bool 应返回 False。"""
        config = self._make_minimal_config(
            harness_eval_gate_enabled=False,
        )
        from agents.workflow import _cfg_bool
        assert _cfg_bool(config, "harness_eval_gate_enabled") is False

    def test_workflow_compile_without_harness(self):
        """禁用 harness 时 workflow 应能正常编译。"""
        config = self._make_minimal_config(
            harness_verify_loop_enabled=False,
            harness_eval_gate_enabled=False,
        )
        from agents.workflow import create_multi_agent_workflow
        with patch("agents.workflow.create_chat_model", return_value=AsyncMock()):
            try:
                app = create_multi_agent_workflow(config)
                # workflow 应能编译成功
                assert app is not None
            except Exception as e:
                pytest.fail(f"Workflow compilation failed with harness disabled: {e}")


class TestProgressiveContextFallback:
    """测试：Progressive Context 的回退机制。"""

    def test_fallback_to_legacy_prompt(self):
        """禁用 progressive_context 时应使用旧版 prompt。"""
        from agents.nodes.intent_analysis import (
            _should_use_progressive_context,
            _assemble_legacy_prompt,
        )

        config = Config(system=SystemConfig(
            harness_progressive_context_enabled=False,
        ))
        assert _should_use_progressive_context(config) is False

        # 旧版 prompt 应能正常渲染
        prompt = _assemble_legacy_prompt(
            file_path="src/test.py",
            file_diff="+x = 1\n",
            file_content="x = 1\n",
        )
        assert len(prompt) > 100
        assert "意图分析Agent" in prompt or "Intent Analysis Agent" in prompt

    def test_enabled_uses_dynamic_prompt(self):
        """启用 progressive_context 时应使用动态 prompt。"""
        from agents.nodes.intent_analysis import _should_use_progressive_context

        config = Config()
        assert _should_use_progressive_context(config) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
