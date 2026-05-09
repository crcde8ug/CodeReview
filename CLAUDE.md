# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run a review (core usage)
python main.py --repo <repo_path> --base <base_branch> --head <head_branch>

# Run batch automated tests
python test/run_automated_tests.py --repos cal.com --cases 1-3
python test/run_automated_tests.py --repos sentry --cases 1-10

# Install dependencies
pip install -r requirements.txt
```

LLM provider is configured via env vars (highest priority) or `config.yaml`:
```bash
export LLM_PROVIDER=openrouter
export LLM_MODEL=deepseek/deepseek-v4-flash
export LLM_API_KEY=your-key
```

## Architecture

**Four-stage LangGraph pipeline** (`agents/workflow.py`):

```
intent_router → intent_analysis (or intent_analysis_chunked) → manager → expert_execution → reporter
```

1. **Intent Analysis** (`agents/nodes/intent_analysis.py`): Map-Reduce — per-file LLM calls in parallel. Degrades to chunked diff-only mode when `changed_files >= 15` or `diff_chars >= 150k`. Each file produces a `FileAnalysis` with `potential_risks: List[RiskItem]`.

2. **Manager** (`agents/nodes/manager.py`): Pure deterministic logic (no LLM call). Takes all `RiskItem`s from intent analysis, applies:
   - **Anchor filtering**: drops risks not within `±manager_anchor_window` lines of changed lines
   - **Near-duplicate merging**: Jaccard similarity on description tokens + line proximity
   - **Budget cap**: scored by `confidence × type_weight × severity_weight`, capped per-file and per-risk-type
   - Outputs `work_list` and `expert_tasks` (grouped by `RiskType`)

3. **Expert Execution** (`agents/nodes/expert_execution.py`): Parallel subgraphs per risk type (`agents/expert_graph.py`). Each expert has a tool-call loop (max rounds = `max_expert_rounds`, max tools = `max_expert_tool_calls`). Tools: `fetch_repo_map`, `read_file`, `run_grep`. Expert prompts are in `agents/prompts/expert_<RiskType>.txt`.

4. **Reporter** (`agents/nodes/reporter.py`): Collects all expert results, filters by `confidence_threshold` (default 0.6, configurable per risk type), calls LLM to generate final markdown report.

### State object (`core/state.py`)

`ReviewState` is a LangGraph `TypedDict` flowing through all nodes:
- **Inputs**: `diff_context`, `changed_files`, `lint_errors`
- **Intermediate**: `file_analyses` → `work_list` + `expert_tasks` → `expert_results`
- **Outputs**: `confirmed_issues`, `final_report`
- **Injected via metadata**: `llm`, `config`, `langchain_tools` — nodes access these via `state["metadata"]["llm"]` etc.

`RiskItem` is the core data model: `(risk_type, file_path, line_number: Tuple[int,int], description, confidence, severity, suggestion)`.

### Six risk types (`core/state.py` `RiskType`)

`Robustness_Boundary_Conditions`, `Concurrency_Timing_Correctness`, `Authorization_Data_Exposure`, `Intent_Semantic_Consistency`, `Lifecycle_State_Consistency`, `Syntax_Static_Errors`

### Pre-agent static analysis (`main.py` `run_syntax_checking`)

Runs before the LangGraph workflow. `external_tools/syntax_checker/` wraps ruff (Python), biome (JS/TS), go vet (Go), PMD (Java). Results are injected as `lint_errors` → converted to `RiskItem`s with `risk_type=Syntax_Static_Errors` and `confidence=0.8`. These bypass anchor filtering.

### Lite-CPG (`lite_cpg/`, `util/lite_cpg_utils.py`)

Per-diff SQLite DB built from tree-sitter ASTs for both base and head revisions. Provides structural context to expert agents. Skipped gracefully if tree-sitter binaries are missing.

### Key configuration knobs (`config.yaml` / `core/config.py`)

| Field | Purpose |
|---|---|
| `confidence_threshold` | Reporter filter (default 0.6) |
| `manager_anchor_window` | ±N lines from changed lines (default 5) |
| `manager_drop_unanchored` | Drop risks far from changes (default true) |
| `manager_max_work_items_total` | Total budget sent to experts (default 15) |
| `manager_max_items_per_risk_type` | Per-type caps |
| `max_expert_rounds` | Circuit breaker for expert tool loops (default 22) |
| `confidence_threshold_by_risk_type` | Per-type reporter thresholds |

### Output structure

```
log/{repo_name}/{model_name}/{base}_2_{head}_{timestamp}/
    review_results_{base}_2_{head}.md
```

### Test datasets

Located in `dataset/{cal.com,keycloak,sentry}-greptile/`. Test cases defined in `test/test_cases.json`; `test/run_automated_tests.py` drives batch evaluation.
