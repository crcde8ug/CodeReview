"""Diff 模式检测器：从 diff 内容中检测可能涉及的风险模式。

用于 Progressive Context：根据检测结果动态组装 intent_analysis 的 prompt，
只注入与当前 diff 相关的模式定义，减少无关信息干扰。
"""

import re
from typing import List

# 各模式对应的关键词（不区分大小写）
_PATTERN_KEYWORDS = {
    "robustness": [
        r"\.get\(",
        r"\.find_by\(",
        r"\.find\(",
        r"\.first\(",
        r"\.last\(",
        r"request\.GET\[",
        r"request\.POST\[",
        r"request\.query_params\[",
        r"\.split\(",
        r"json\.loads?\(",
        r"\?\?",
        r"\|\|\s*\w",
        r"if\s+\w+\s*:",
        r"if\s*\(\s*\w+\s*\)",
        r"null",
        r"None",
        r"undefined",
        r"IndexError",
        r"KeyError",
        r"try\s*:",
        r"try\s*\{",
        r"catch\s*\(",
        r"except\s+",
        r"\[0\]",
        r"\[-1\]",
        r"\?\.",
    ],
    "concurrency": [
        r"\basync\b",
        r"\bawait\b",
        r"\.forEach\s*\(",
        r"\.map\s*\(",
        r"\bPromise\b",
        r"Promise\.all",
        r"setTimeout",
        r"setInterval",
        r"\bThread\b",
        r"\bthreading\b",
        r"Lock\b",
        r"Semaphore\b",
        r"synchronized\b",
        r"\bmutex\b",
        r"\bgoroutine\b",
        r"\bchannel\b",
        r"asyncio\.",
        r"async\s+def\s+",
        r"async\s+with\s+",
        r"async\s+for\s+",
        r"\batomic\b",
        r"\brace\b",
    ],
    "authorization": [
        r"sql\(",
        r"\.raw\(",
        r"\bexec\(",
        r"\beval\(",
        r"innerHTML",
        r"dangerouslySetInnerHTML",
        r"\bsystem\(",
        r"\bopen\(\s*[\"']?\|",
        r"\bfetch\(",
        r"axios\.",
        r"http\.Get",
        r"http\.Post",
        r"password",
        r"\btoken\b",
        r"\bsecret\b",
        r"\bapi_key\b",
        r"\bapi_key\b",
        r"hasPermission",
        r"canManage",
        r"isAdmin",
        r"isOwner",
        r"authorize",
        r"authenticate",
        r"\.user\.id",
        r"\.user\.role",
        r"Prisma\.sql",
        r"f\".*\bSELECT\b",
        r"f\".*\bINSERT\b",
        r"f\".*\bUPDATE\b",
        r"f\".*\bDELETE\b",
    ],
    "intent": [
        r"&&\s*[^&]",
        r"\|\|\s*[^|]",
        r"if.*&&.*\|\|",
        r"if.*\|\|.*&&",
        r"\bstart_date\b",
        r"\bend_date\b",
        r"\bstart_time\b",
        r"\bend_time\b",
        r"timedelta",
        r"dateutil",
        r"moment\(",
        r"dayjs\(",
        r"\bcount\b.*[+\-*/]",
        r"[+\-*/].*\bcount\b",
        r"\bratio\b",
        r"\baverage\b",
        r"\bsum\b",
        r"feature.*flag",
        r"featureFlag",
        r"feature_flag",
        r"toggle",
        r"is_enabled",
    ],
    "lifecycle": [
        r"updateMany\(",
        r"\.update\(",
        r"\.delete\(",
        r"\.destroy\(",
        r"where:\s*\{\s*\}",
        r"def\s+\w+\(.*=\s*\{",
        r"def\s+\w+\(.*=\s*\[",
        r"def\s+\w+\(.*=\s*datetime",
        r"def\s+\w+\(.*=\s*now\(",
        r"def\s+\w+\(.*=\s*timezone\.now\(\)",
        r"addEventListener\(",
        r"removeEventListener\(",
        r"subscribe\(",
        r"unsubscribe\(",
        r"setInterval\(",
        r"clearInterval\(",
        r"useEffect\s*\(",
        r"componentWillUnmount",
        r"__init__\(",
        r"__del__\(",
        r"\.close\(",
        r"\.dispose\(",
        r"\.cleanup\(",
        r"feature.*flag",
        r"featureFlag",
        r"self\.\w+\(",
        r"def\s+\w+\(self.*\):\s*\n\s*self\.\w+\(",
    ],
}

# 模式名到 prompt 文件名的映射
_PATTERN_TO_FILE = {
    "robustness": "robustness",
    "concurrency": "concurrency",
    "authorization": "authorization",
    "intent": "intent",
    "lifecycle": "lifecycle",
}


def detect_patterns_from_diff(diff_content: str) -> List[str]:
    """从 diff 内容中检测可能涉及的风险模式。

    Args:
        diff_content: Git diff 字符串。

    Returns:
        检测到的模式列表（去重），如 ["robustness", "concurrency"]。
        如果未检测到任何模式，默认返回所有模式（保守策略）。
    """
    if not diff_content or not diff_content.strip():
        return list(_PATTERN_TO_FILE.keys())

    detected = set()
    for pattern_name, keywords in _PATTERN_KEYWORDS.items():
        for kw in keywords:
            if re.search(kw, diff_content, re.IGNORECASE):
                detected.add(pattern_name)
                break

    if not detected:
        return list(_PATTERN_TO_FILE.keys())

    return list(detected)


def load_pattern_text(pattern_name: str) -> str:
    """加载指定模式的 prompt 文本。

    Args:
        pattern_name: 模式名，如 "robustness"、"concurrency" 等。

    Returns:
        模式文件的文本内容，如果文件不存在则返回空字符串。
    """
    file_name = _PATTERN_TO_FILE.get(pattern_name)
    if not file_name:
        return ""

    from pathlib import Path
    pattern_path = Path(__file__).resolve().parent.parent / "agents" / "prompts" / "patterns" / f"{file_name}.txt"
    if not pattern_path.exists():
        return ""

    try:
        return pattern_path.read_text(encoding="utf-8")
    except Exception:
        return ""
