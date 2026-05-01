"""OpenRouter LLM 接口测试。

使用环境变量 LLM_API_KEY 作为 API Key。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import LLMConfig
from core.llm_factory import create_chat_model
from langchain_core.messages import HumanMessage


def test_openrouter_basic():
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        print("SKIP: LLM_API_KEY not set")
        return

    config = LLMConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
        api_key=api_key,
        temperature=0,
    )
    llm = create_chat_model(config)
    response = llm.invoke([HumanMessage(content="Say 'hello' in one word.")])
    print(f"Response: {response.content}")
    assert response.content.strip(), "Empty response from OpenRouter"
    print("PASS: OpenRouter basic test")


if __name__ == "__main__":
    test_openrouter_basic()
