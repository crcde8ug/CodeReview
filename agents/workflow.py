"""基于 LangGraph 的多智能体代码审查工作流。

工作流结构：
1. Intent Analysis（Map-Reduce）：并行分析文件意图（或大 PR 降级为 chunked diff-only）
2. Manager：生成任务列表并按风险类型分组
3. Expert Execution：并行执行专家组任务（并发控制）
4. Reporter：生成最终报告
"""

import logging
from typing import Dict, Any, List, Optional
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode
from langchain_core.messages import HumanMessage
from langchain_core.language_models import BaseChatModel
from core.state import ReviewState
from core.llm_factory import create_chat_model
from core.config import Config
from tools.langchain_tools import create_tools_with_context
from agents.nodes.intent_analysis_chunked import intent_analysis_chunked_node
from agents.nodes.intent_analysis import intent_analysis_node
from agents.nodes.verify_loop import verify_loop_node
from agents.nodes.manager import manager_node
from agents.nodes.expert_execution import expert_execution_node
from agents.nodes.eval_gate import eval_gate_node
from agents.nodes.reporter import reporter_node
from util.expert_stats import format_tool_call_summary
from util.runtime_utils import ensure_run_started, elapsed_seconds, elapsed_tag, format_duration

logger = logging.getLogger(__name__)

def _env_int(name: str, default: int) -> int:
    import os
    v = (os.getenv(name) or "").strip()
    if not v:
        return int(default)
    try:
        return int(v)
    except Exception:
        return int(default)


def _should_use_chunked_intent(state: ReviewState) -> bool:
    """Corner-case degradation decision: avoid per-file LLM calls on huge PRs."""
    diff_context = state.get("diff_context", "") or ""
    changed_files = state.get("changed_files", []) or []
    file_threshold = _env_int("INTENT_DEGRADE_FILE_THRESHOLD", 15)
    diff_chars_threshold = _env_int("INTENT_DEGRADE_DIFF_CHARS_THRESHOLD", 150_000)
    return (len(changed_files) >= file_threshold) or (len(diff_context) >= diff_chars_threshold)


async def intent_router_node(state: ReviewState) -> ReviewState:
    """No-op node that records the intent mode decision into metadata."""
    meta = dict(state.get("metadata") or {})
    mode = "chunked" if _should_use_chunked_intent(state) else "per_file"
    meta["intent_mode"] = mode
    meta["intent_router"] = {
        "changed_files": len(state.get("changed_files", []) or []),
        "diff_chars": len(state.get("diff_context", "") or ""),
    }
    return {"metadata": meta}


def route_to_intent(state: ReviewState) -> str:
    """Route to per-file intent or chunked diff-only intent based on PR size."""
    mode = (state.get("metadata") or {}).get("intent_mode")
    if mode == "chunked":
        return "intent_analysis_chunked"
    return "intent_analysis"


def _cfg_bool(cfg: Config, name: str) -> bool:
    """从 SystemConfig 读取布尔配置项，默认 true。"""
    try:
        return bool(getattr(cfg.system, name, True))
    except Exception:
        return True


def create_multi_agent_workflow(
    config: Config,
    enable_checkpointing: bool = False
) -> Any:
    """创建多智能体工作流图。

    Args:
        config: 配置对象。
        enable_checkpointing: 是否启用 checkpointer（默认禁用）。

    Returns:
        编译后的 LangGraph 工作流。
    """
    # Initialize LLM using factory function
    llm = create_chat_model(config.llm)

    workspace_root = config.system.workspace_root
    asset_key = config.system.asset_key

    langchain_tools = create_tools_with_context(
        workspace_root=workspace_root,
        asset_key=asset_key
    )

    checkpointer = MemorySaver() if enable_checkpointing else None

    # Read harness flags
    use_verify_loop = _cfg_bool(config, "harness_verify_loop_enabled")
    use_eval_gate = _cfg_bool(config, "harness_eval_gate_enabled")

    if not use_verify_loop:
        print("  [Harness] verify_loop disabled")
    if not use_eval_gate:
        print("  [Harness] eval_gate disabled")

    # Create workflow graph
    workflow = StateGraph(ReviewState)

    # Always-add nodes
    workflow.add_node("intent_router", intent_router_node)
    workflow.add_node("intent_analysis", intent_analysis_node)
    workflow.add_node("intent_analysis_chunked", intent_analysis_chunked_node)
    workflow.add_node("manager", manager_node)
    workflow.add_node("expert_execution", expert_execution_node)
    workflow.add_node("reporter", reporter_node)

    # Conditionally add harness nodes
    if use_verify_loop:
        workflow.add_node("verify_loop", verify_loop_node)
    if use_eval_gate:
        workflow.add_node("eval_gate", eval_gate_node)

    # Set entry point
    workflow.set_entry_point("intent_router")

    # Add edges
    # Intent Router -> Intent Analysis (per-file) or Chunked Intent (conditional)
    workflow.add_conditional_edges(
        "intent_router",
        route_to_intent,
        {
            "intent_analysis": "intent_analysis",
            "intent_analysis_chunked": "intent_analysis_chunked",
        }
    )

    # Intent Analysis → [verify_loop] → Manager
    if use_verify_loop:
        workflow.add_edge("intent_analysis", "verify_loop")
        workflow.add_edge("intent_analysis_chunked", "verify_loop")
        workflow.add_edge("verify_loop", "manager")
    else:
        workflow.add_edge("intent_analysis", "manager")
        workflow.add_edge("intent_analysis_chunked", "manager")

    # Manager → Expert Execution or Reporter (conditional)
    workflow.add_conditional_edges(
        "manager",
        route_to_experts,
        {
            "expert_execution": "expert_execution",
            "reporter": "reporter"
        }
    )

    # Expert Execution → [eval_gate] → Reporter
    if use_eval_gate:
        workflow.add_edge("expert_execution", "eval_gate")
        workflow.add_edge("eval_gate", "reporter")
    else:
        workflow.add_edge("expert_execution", "reporter")

    # Reporter → END
    workflow.add_edge("reporter", END)

    # Compile workflow with checkpointer
    compile_kwargs = {}
    if checkpointer:
        compile_kwargs["checkpointer"] = checkpointer

    compiled = workflow.compile(**compile_kwargs)

    # Wrap nodes to inject dependencies
    return _wrap_workflow_with_dependencies(
        compiled,
        llm,
        config,
        langchain_tools
    )


def route_to_experts(state: ReviewState) -> str:
    """从 Manager 路由到 expert_execution 或 reporter。
    
    如果 work_list 为空，跳转到 reporter；否则执行 expert_execution。
    """
    print("\n" + "="*80)
    meta = state.get("metadata") or {}
    print(f"🔀 [路由] route_to_experts - 决策下一步 ({elapsed_tag(meta)})")
    print("="*80)
    
    work_list = state.get("work_list", [])
    expert_tasks = state.get("expert_tasks", {})
    
    if not work_list or not expert_tasks:
        print("  ⏭️  无任务，跳过专家执行，直接生成报告")
        logger.info("No work list or expert tasks, skipping to reporter")
        return "reporter"
    
    print(f"  ➡️  路由到 expert_execution")
    print(f"     - 专家组数: {len(expert_tasks)}")
    print("="*80)
    logger.info(f"Routing to expert_execution with {len(expert_tasks)} expert groups")
    return "expert_execution"


def _wrap_workflow_with_dependencies(
    compiled_graph: Any,
    llm: BaseChatModel,
    config: Config,
    langchain_tools: List[Any]
) -> Any:
    """包装工作流节点以注入依赖（LLM、配置、工具）。
    
    通过修改 state 的 metadata 字段注入依赖。
    """
    # Store original invoke methods
    original_ainvoke = compiled_graph.ainvoke
    original_invoke = compiled_graph.invoke
    
    async def ainvoke_with_deps(state: ReviewState, **kwargs) -> ReviewState:
        """执行工作流（注入依赖到 state）。"""
        if "messages" not in state:
            state["messages"] = []
        
        if "metadata" not in state:
            state["metadata"] = {}
        
        state["metadata"]["llm"] = llm
        state["metadata"]["config"] = config
        state["metadata"]["langchain_tools"] = langchain_tools
        
        # Call original invoke
        return await original_ainvoke(state, **kwargs)
    
    def invoke_with_deps(state: ReviewState, **kwargs) -> ReviewState:
        """执行工作流（同步版本）。"""
        if "messages" not in state:
            state["messages"] = []
        
        if "metadata" not in state:
            state["metadata"] = {}
        
        state["metadata"]["llm"] = llm
        state["metadata"]["config"] = config
        state["metadata"]["langchain_tools"] = langchain_tools
        
        # Call original invoke
        return original_invoke(state, **kwargs)
    
    # Replace methods
    compiled_graph.ainvoke = ainvoke_with_deps
    compiled_graph.invoke = invoke_with_deps
    
    return compiled_graph


async def map_intent_analysis(state: ReviewState) -> ReviewState:
    """意图分析的 Map 函数（在 intent_analysis_node 中实现并行执行）。"""
    # For now, we'll process all files in the intent_analysis_node
    # In a full implementation, we could use LangGraph's map capabilities
    # or implement custom parallel execution
    return await intent_analysis_node(state)


async def run_multi_agent_workflow(
    diff_context: str,
    changed_files: List[str],
    config: Config = None,
    lint_errors: List[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """运行多智能体代码审查工作流。
    
    Args:
        diff_context: Git diff 字符串。
        changed_files: 变更文件路径列表。
        config: 配置对象（可选，默认使用默认配置）。
        lint_errors: 预检查的 lint 错误列表（可选）。
    
    Returns:
        包含最终审查结果的状态字典。
    """
    if config is None:
        from core.config import Config
        config = Config.load_default()
    
    # Create workflow
    app = create_multi_agent_workflow(config)
    
    # Initialize state
    confidence_threshold = 0.6
    try:
        confidence_threshold = float(getattr(getattr(config, "system", None), "confidence_threshold", 0.6))
    except Exception:
        confidence_threshold = 0.6

    initial_state: ReviewState = {
        "messages": [],
        "diff_context": diff_context,
        "changed_files": changed_files,
        "file_analyses": [],
        "work_list": [],
        "expert_tasks": {},
        "expert_results": {},
        "confirmed_issues": [],
        "final_report": "",
        "lint_errors": lint_errors or [],
        "metadata": {
            "workflow_version": "multi_agent_parallel",
            "config_provider": config.llm.provider,
            "confidence_threshold": confidence_threshold
        }
    }
    ensure_run_started(initial_state["metadata"])
    
    # Run the workflow
    print("\n" + "="*80)
    print("🚀 多智能体工作流启动")
    print("="*80)
    print(f"📝 输入 ({elapsed_tag(initial_state['metadata'])}):")
    print(f"   - Diff 上下文: {len(diff_context)} 字符")
    print(f"   - 变更文件数: {len(changed_files)}")
    print(f"   - Lint 错误数: {len(lint_errors) if lint_errors else 0}")
    print("="*80)
    
    try:
        invoke_kwargs = {}
        
        # 可选：如果启用了 checkpointer，生成 thread_id 并传入 config
        # import hashlib
        # import time
        # thread_id = hashlib.md5(
        #     f"{time.time()}_{','.join(changed_files)}".encode()
        # ).hexdigest()[:16]
        # invoke_kwargs['config'] = {
        #     "configurable": {
        #         "thread_id": thread_id
        #     }
        # }
        
        final_state = await app.ainvoke(initial_state, **invoke_kwargs)
        
        meta = final_state.get("metadata") or {}
        ensure_run_started(meta)
        total_s = elapsed_seconds(meta)
        print("\n" + "="*80)
        print(f"✅ 工作流执行完成 ({elapsed_tag(meta)})")
        print("="*80)
        print(f"📊 最终结果:")
        print(f"   - 确认问题数: {len(final_state.get('confirmed_issues', []))}")
        print(f"   - 报告长度: {len(final_state.get('final_report', ''))} 字符")
        print(f"⏱️ 总运行时间: {format_duration(total_s)}")
        print("="*80)

        stats = (final_state.get("metadata") or {}).get("expert_tool_call_stats")
        if isinstance(stats, dict):
            print(format_tool_call_summary(stats))
        
        return final_state
    except Exception as e:
        logger.error(f"Workflow execution error: {e}", exc_info=True)
        # Error handling: return error state
        return {
            **initial_state,
            "confirmed_issues": [],
            "final_report": f"Workflow execution error: {str(e)}",
            "metadata": {
                **initial_state.get("metadata", {}),
                "workflow_error": str(e)
            }
        }
