"""Eval Gate 节点：独立质量门验证专家结论。

通用机制：对每个专家产出的 RiskItem，从不同角度做反证搜索，
分 confirmed/disputed/uncertain 三档调整 confidence。
插入在 expert_execution 和 reporter 之间。
"""

import asyncio
import json
import logging
from typing import Dict, Any, List

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel

from core.state import ReviewState, RiskItem
from util.file_utils import read_file_content
from util.diff_utils import extract_file_diff
from util.json_utils import extract_json_from_text
from util.runtime_utils import elapsed_tag

logger = logging.getLogger(__name__)

_VERDICT_MAP = {
    "confirmed": lambda expert_conf: max(float(expert_conf), 0.7),
    "disputed": lambda _expert_conf: 0.3,
    "uncertain": lambda expert_conf: float(expert_conf) * 0.8,
}


def _build_eval_prompt(
    file_path: str,
    risk_type: str,
    risk_item: RiskItem,
    diff_context: str,
    file_content: str,
) -> str:
    """组装 eval_gate prompt。"""
    start_line, end_line = risk_item.line_number
    return (
        f"你是一名\"独立评审员\"。你的任务是对另一位专家的审查结论进行交叉验证。"
        f"你不是重复专家的工作，而是从不同的角度寻找反证。\n"
        f"\n"
        f"## 输入\n"
        f"- **File Path**: {file_path}\n"
        f"- **Risk Type**: {risk_type}\n"
        f"- **Expert Conclusion**: {risk_item.description}\n"
        f"- **Expert Confidence**: {risk_item.confidence}\n"
        f"- **Line Number**: [{start_line}, {end_line}]\n"
        f"- **Diff Context**:\n{diff_context}\n"
        f"- **File Content**:\n{file_content}\n"
        f"\n"
        f"## 验证流程（必须逐步执行）\n"
        f"\n"
        f"1. **可证伪断言**：将专家结论改写为一句\"如果 X 成立，则在代码 Y 处应观察到 Z\"的断言。\n"
        f"2. **反向搜索**：从与专家不同的角度寻找反证：\n"
        f"   - 如果专家说\"缺少判空保护\"，你去搜索\"是否存在判空/卫语句/默认值/try-catch\"\n"
        f"   - 如果专家说\"会抛 KeyError/IndexError\"，你去搜索\"该 key 是否确实存在/是否有 .get() 保护\"\n"
        f"   - 如果专家说\"存在并发竞态\"，你去搜索\"是否有锁/信号量/队列/序列化机制\"\n"
        f"   - 如果专家说\"鉴权缺失\"，你去搜索\"是否有上游中间件/装饰器/框架层保护\"\n"
        f"3. **结论判定**：\n"
        f"   - confirmed：没找到反证，专家结论成立\n"
        f"   - disputed：找到明确反证，专家结论不成立或显著夸大\n"
        f"   - uncertain：没找到反证但证据也不充分，无法确认\n"
        f"\n"
        f"## 输出格式\n"
        f"\n"
        f"只输出 JSON，不要任何解释性文字。JSON 必须包含以下字段：\n"
        f'{{"verdict": "confirmed"/"disputed"/"uncertain", "reason": "一句话说明", "final_confidence": 0.0-1.0}}\n'
        f"\n"
        f"## confidence 调整规则\n"
        f"- verdict = confirmed → final_confidence = max(专家confidence, 0.7)\n"
        f"- verdict = disputed → final_confidence = 0.3\n"
        f"- verdict = uncertain → final_confidence = 专家confidence × 0.8\n"
    )


async def eval_gate_node(state: ReviewState) -> Dict[str, Any]:
    """对专家产出的所有 RiskItem 做独立交叉验证。

    每个 RiskItem 调用一次 LLM 做反证搜索，根据 verdict 调整 confidence。

    Returns:
        包含更新后的 'expert_results' 键的字典。
    """
    print("\n" + "=" * 80)
    meta = state.get("metadata") or {}
    print(f"🛡️ [节点3.5] Eval Gate - 独立质量验证 ({elapsed_tag(meta)})")
    print("=" * 80)

    llm: BaseChatModel = state.get("metadata", {}).get("llm")
    if not llm:
        logger.error("LLM not found in metadata, skipping eval_gate")
        return {"expert_results": state.get("expert_results", {})}

    config = state.get("metadata", {}).get("config")
    max_concurrent = getattr(getattr(config, "system", None), "max_concurrent_llm_requests", 5)

    expert_results_dicts: Dict[str, List[Dict[str, Any]]] = state.get("expert_results", {})
    if not expert_results_dicts:
        print("  ⚠️  没有专家结果，跳过 eval_gate")
        return {"expert_results": {}}

    # Convert to RiskItem objects
    expert_results: Dict[str, List[RiskItem]] = {}
    for risk_type_str, items in expert_results_dicts.items():
        expert_results[risk_type_str] = [
            RiskItem(**item) if isinstance(item, dict) else item
            for item in items
        ]

    # Collect all risk items with their type
    all_risks: List[tuple[str, int, RiskItem]] = []
    for risk_type_str, items in expert_results.items():
        for j, risk in enumerate(items):
            all_risks.append((risk_type_str, j, risk))

    total = len(all_risks)
    print(f"  📥 接收专家结果: {total} 个风险项（{len(expert_results)} 个风险类型）")
    print(f"  🔒 并发控制: Semaphore(max={max_concurrent})")

    semaphore = asyncio.Semaphore(max_concurrent)

    verdict_counts: Dict[str, int] = {"confirmed": 0, "disputed": 0, "uncertain": 0}
    total_adjusted = 0.0

    async def eval_single_risk(
        risk_type_str: str,
        risk_idx: int,
        risk: RiskItem,
    ) -> tuple[str, int, RiskItem]:
        nonlocal total_adjusted

        async with semaphore:
            file_path = risk.file_path
            file_content = read_file_content(file_path, config)
            diff_for_file = extract_file_diff(
                state.get("diff_context", ""), file_path
            )

            prompt = _build_eval_prompt(
                file_path, risk_type_str, risk, diff_for_file, file_content
            )

            messages = [
                SystemMessage(
                    content="You are an independent code review evaluator. Respond with valid JSON only."
                ),
                HumanMessage(content=prompt),
            ]

            try:
                response = await llm.ainvoke(messages)
                response_text = response.content if hasattr(response, "content") else str(response)

                json_text = extract_json_from_text(response_text) or response_text
                result = json.loads(json_text)

                verdict = str(result.get("verdict", "uncertain")).lower()
                if verdict not in _VERDICT_MAP:
                    verdict = "uncertain"

                expert_conf = risk.confidence
                final_confidence = _VERDICT_MAP[verdict](expert_conf)
                final_confidence = max(0.0, min(1.0, final_confidence))

                updated_risk = RiskItem(
                    risk_type=risk.risk_type,
                    file_path=risk.file_path,
                    line_number=risk.line_number,
                    description=risk.description,
                    confidence=final_confidence,
                    severity=risk.severity,
                    suggestion=risk.suggestion,
                )

                verdict_counts[verdict] += 1
                total_adjusted += abs(final_confidence - expert_conf)

                return (risk_type_str, risk_idx, updated_risk)

            except Exception as e:
                logger.warning(
                    f"Eval failed for risk {risk.file_path}:{risk.line_number}: "
                    f"{type(e).__name__}: {e}"
                )
                # On error, keep original risk with slight penalty
                updated_risk = RiskItem(
                    risk_type=risk.risk_type,
                    file_path=risk.file_path,
                    line_number=risk.line_number,
                    description=risk.description,
                    confidence=max(risk.confidence * 0.95, 0.3),
                    severity=risk.severity,
                    suggestion=risk.suggestion,
                )
                return (risk_type_str, risk_idx, updated_risk)

    # Process all risks concurrently
    print(f"\n  🚀 开始验证 {total} 个专家结果...")
    results = await asyncio.gather(
        *[eval_single_risk(rt, idx, r) for rt, idx, r in all_risks]
    )

    # Apply updates back to expert_results
    for risk_type_str, risk_idx, updated_risk in results:
        if risk_type_str in expert_results and risk_idx < len(expert_results[risk_type_str]):
            expert_results[risk_type_str][risk_idx] = updated_risk

    # Convert back to dicts for state
    expert_results_out = {
        rt: [item.model_dump() for item in items]
        for rt, items in expert_results.items()
    }

    avg_adjustment = total_adjusted / total if total else 0
    print(f"\n  ✅ Eval Gate 完成! ({elapsed_tag(meta)})")
    print(f"     - 总验证数: {total}")
    print(f"     - confirmed: {verdict_counts['confirmed']}")
    print(f"     - disputed: {verdict_counts['disputed']}")
    print(f"     - uncertain: {verdict_counts['uncertain']}")
    print(f"     - 平均 confidence 调整幅度: {avg_adjustment:.3f}")

    # Print per-type summary
    for rt, items in expert_results.items():
        n_confirmed = sum(1 for i in items if i.confidence >= 0.7)
        n_disputed = sum(1 for i in items if i.confidence <= 0.3)
        n_uncertain = len(items) - n_confirmed - n_disputed
        print(f"     • {rt}: {len(items)} 项 — confirmed={n_confirmed}, disputed={n_disputed}, uncertain={n_uncertain}")

    print("=" * 80)

    return {"expert_results": expert_results_out}
