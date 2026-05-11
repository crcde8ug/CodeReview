"""Verify Loop 节点：验证意图分析阶段产生的 RiskItem 是否有代码证据锚点。

通用机制：每个 RiskItem 必须指向一行具体代码作为证据，否则降低置信度。
插入在 intent_analysis 和 manager 之间。
"""

import asyncio
import json
import logging
from typing import Dict, Any, List

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel

from core.state import ReviewState, RiskItem, FileAnalysis
from util.file_utils import read_file_content
from util.json_utils import extract_json_from_text
from util.runtime_utils import elapsed_tag

logger = logging.getLogger(__name__)


def _build_verify_prompt(
    file_path: str,
    risk_item: RiskItem,
    file_content: str,
) -> str:
    """组装 verify_loop prompt，不依赖 render_prompt_template 以节省 LLM 调用开销。"""
    start_line, end_line = risk_item.line_number
    return (
        f"你是一名\"代码证据验证器\"。你的任务不是深度分析，而是验证一个风险描述是否在当前文件的具体代码行上有证据支撑。\n"
        f"\n"
        f"## 输入\n"
        f"- **File Path**: {file_path}\n"
        f"- **Risk Type**: {risk_item.risk_type.value}\n"
        f"- **Risk Description**: {risk_item.description}\n"
        f"- **Claimed Line Number**: [{start_line}, {end_line}]\n"
        f"- **File Content**:\n{file_content}\n"
        f"\n"
        f"## 验证规则\n"
        f"\n"
        f"1. 读取 line_number 指定的代码行范围。\n"
        f"2. 判断该位置的代码是否直接支撑 risk_description 所描述的问题。\n"
        f"3. 锚定标准（必须同时满足）：\n"
        f"   - 代码行确实存在于 file_content 中（行号在范围内）\n"
        f"   - 代码行内容与风险描述语义相关（例如风险说\"缺少判空\"，代码行上确实有 dereference 操作）\n"
        f"   - 风险描述中提到的符号/变量/方法名在代码行中可见\n"
        f"4. 如果 line_number 超出 file_content 范围 → 标记为未锚定。\n"
        f"5. 如果风险描述过于笼统（如\"可能存在性能问题\"但没有指向具体操作）→ 标记为未锚定。\n"
        f"\n"
        f"## 输出格式\n"
        f"\n"
        f"只输出 JSON，不要任何解释性文字。JSON 必须包含以下字段：\n"
        f"{{\"anchored\": true 或 false, \"evidence\": \"一句话说明\", \"adjusted_confidence\": 0.0-1.0}}\n"
        f"\n"
        f"## confidence 调整规则\n"
        f"- anchored=true 且证据直接明确 → adjusted_confidence 保持原值不变\n"
        f"- anchored=true 但证据间接 → adjusted_confidence 设为原值的 0.8 倍\n"
        f"- anchored=false → adjusted_confidence 设为 0.3\n"
    )


async def verify_loop_node(state: ReviewState) -> Dict[str, Any]:
    """验证意图分析阶段产生的所有 RiskItem 是否有代码证据锚点。

    对每个 RiskItem，读取其 file_path 的内容，调用 LLM 验证该 RiskItem
    是否在指定的 line_number 处有直接代码证据。无证据的项 confidence 降至 0.3。

    Returns:
        包含更新后的 'file_analyses' 键的字典。
    """
    print("\n" + "=" * 80)
    meta = state.get("metadata") or {}
    print(f"🔎 [节点1.5] Verify Loop - 验证风险证据锚点 ({elapsed_tag(meta)})")
    print("=" * 80)

    llm: BaseChatModel = state.get("metadata", {}).get("llm")
    if not llm:
        logger.error("LLM not found in metadata, skipping verify_loop")
        return {"file_analyses": state.get("file_analyses", [])}

    config = state.get("metadata", {}).get("config")
    max_concurrent = getattr(getattr(config, "system", None), "max_concurrent_llm_requests", 5)
    confidence_floor = float(
        getattr(getattr(config, "system", None), "harness_verify_confidence_floor", 0.3)
    )

    file_analyses_dicts = state.get("file_analyses", [])
    if not file_analyses_dicts:
        print("  ⚠️  没有文件分析结果，跳过 verify_loop")
        return {"file_analyses": []}

    file_analyses = [
        FileAnalysis(**fa) if isinstance(fa, dict) else fa
        for fa in file_analyses_dicts
    ]

    # Collect all risk items
    all_risks: List[tuple[int, int, RiskItem]] = []
    for i, fa in enumerate(file_analyses):
        for j, risk in enumerate(fa.potential_risks or []):
            all_risks.append((i, j, risk))

    if not all_risks:
        print("  ⚠️  没有风险项需要验证，跳过 verify_loop")
        return {"file_analyses": file_analyses_dicts}

    print(f"  📥 接收风险项: {len(all_risks)} 个（来自 {len(file_analyses)} 个文件）")
    print(f"  🔒 并发控制: Semaphore(max={max_concurrent})")
    print(f"  📏 未锚定 confidence 下限: {confidence_floor}")

    semaphore = asyncio.Semaphore(max_concurrent)

    anchored_count = 0
    unanchored_count = 0
    total_adjusted = 0.0

    async def verify_single_risk(
        fa_idx: int, risk_idx: int, risk: RiskItem
    ) -> tuple[int, int, RiskItem]:
        nonlocal anchored_count, unanchored_count, total_adjusted

        async with semaphore:
            file_path = risk.file_path
            file_content = read_file_content(file_path, config)

            prompt = _build_verify_prompt(file_path, risk, file_content)

            messages = [
                SystemMessage(
                    content="You are a code evidence verifier. Respond with valid JSON only."
                ),
                HumanMessage(content=prompt),
            ]

            try:
                response = await llm.ainvoke(messages)
                response_text = response.content if hasattr(response, "content") else str(response)

                json_text = extract_json_from_text(response_text) or response_text
                result = json.loads(json_text)

                anchored = bool(result.get("anchored", False))
                adjusted_confidence = float(result.get("adjusted_confidence", risk.confidence))
                evidence = result.get("evidence", "")

                # Apply confidence floor for unanchored items
                if not anchored:
                    adjusted_confidence = min(adjusted_confidence, confidence_floor)

                # Create updated risk item
                updated_risk = RiskItem(
                    risk_type=risk.risk_type,
                    file_path=risk.file_path,
                    line_number=risk.line_number,
                    description=risk.description,
                    confidence=adjusted_confidence,
                    severity=risk.severity,
                    suggestion=risk.suggestion,
                )

                total_adjusted += abs(updated_risk.confidence - risk.confidence)

                if anchored:
                    anchored_count += 1
                else:
                    unanchored_count += 1

                return (fa_idx, risk_idx, updated_risk)

            except Exception as e:
                logger.warning(
                    f"Verify failed for risk {risk.file_path}:{risk.line_number}: "
                    f"{type(e).__name__}: {e}"
                )
                # On error, keep original risk but lower confidence slightly
                updated_risk = RiskItem(
                    risk_type=risk.risk_type,
                    file_path=risk.file_path,
                    line_number=risk.line_number,
                    description=risk.description,
                    confidence=max(risk.confidence * 0.9, 0.3),
                    severity=risk.severity,
                    suggestion=risk.suggestion,
                )
                return (fa_idx, risk_idx, updated_risk)

    # Process all risks concurrently
    print(f"\n  🚀 开始验证 {len(all_risks)} 个风险项...")
    results = await asyncio.gather(
        *[verify_single_risk(i, j, r) for i, j, r in all_risks]
    )

    # Apply updates back to file analyses
    for fa_idx, risk_idx, updated_risk in results:
        if fa_idx < len(file_analyses):
            fa = file_analyses[fa_idx]
            if fa.potential_risks and risk_idx < len(fa.potential_risks):
                fa.potential_risks[risk_idx] = updated_risk

    # Build output
    updated_file_analyses = [fa.model_dump() for fa in file_analyses]

    avg_adjustment = total_adjusted / len(all_risks) if all_risks else 0
    print(f"\n  ✅ Verify Loop 完成! ({elapsed_tag(meta)})")
    print(f"     - 总验证数: {len(all_risks)}")
    print(f"     - 已锚定: {anchored_count}")
    print(f"     - 未锚定: {unanchored_count}")
    print(f"     - 平均 confidence 调整幅度: {avg_adjustment:.3f}")
    print("=" * 80)

    return {"file_analyses": updated_file_analyses}
