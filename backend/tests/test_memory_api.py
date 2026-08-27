"""记忆查询接口的纯函数测试（不做网络/SSH 调用）。

覆盖：
- _extract_thread_messages：user/assistant 文本提取、ToolMessage 跳过、
  messages+ 通道兼容、空通道
- _title_from_thread_messages：取首条 user 消息、超长截断、无文本回退

运行：uv run pytest -x tests/test_memory_api.py -v
"""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deploy_agent.api import (
    _extract_thread_messages,
    _title_from_thread_messages,
)


def test_extract_thread_messages_basic():
    """正常序列：user → assistant（文本）→ tool 跳过 → user。"""
    values = {
        "messages": [
            HumanMessage(content="部署 develop 分支"),
            AIMessage(content="好的，开始部署"),
            ToolMessage(content=json.dumps({"success": True}), tool_call_id="c1"),
            HumanMessage(content="继续"),
        ]
    }
    assert _extract_thread_messages(values) == [
        {"role": "user", "content": "部署 develop 分支"},
        {"role": "assistant", "content": "好的，开始部署"},
        {"role": "user", "content": "继续"},
    ]


def test_extract_thread_messages_messages_plus_channel():
    """新版通道名 messages+ 也要能读取。"""
    values = {
        "messages+": [
            HumanMessage(content="你好"),
            AIMessage(content="在的"),
        ]
    }
    assert _extract_thread_messages(values) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "在的"},
    ]


def test_extract_thread_messages_empty_channel():
    """空通道返回空列表。"""
    assert _extract_thread_messages({}) == []
    assert _extract_thread_messages({"messages": []}) == []


def test_extract_thread_messages_ai_content_list():
    """AIMessage content 为 list（含 tool 调用块）时只提取文本块。"""
    ai = AIMessage(
        content=[
            {"type": "text", "text": "正在调用工具"},
            {"type": "tool_use", "id": "c1", "name": "git_pull_code", "input": {}},
        ]
    )
    assert _extract_thread_messages({"messages": [ai]}) == [
        {"role": "assistant", "content": "正在调用工具"}
    ]


def test_title_from_thread_messages_takes_first_user_text():
    """标题取第一条非空 user 消息（短消息不截断）。"""
    msgs = [
        HumanMessage(content=""),
        AIMessage(content="在的"),
        HumanMessage(content="部署 develop 分支"),
    ]
    assert _title_from_thread_messages(msgs) == "部署 develop 分支"


def test_title_from_thread_messages_truncated():
    """超长标题截断到 20 字并加省略号。"""
    long_text = "这是一条非常非常长的部署指令用来测试标题截断逻辑是否正常工作"
    assert len(long_text) > 20
    title = _title_from_thread_messages([HumanMessage(content=long_text)])
    assert len(title) == 21  # 20 字 + …
    assert title.endswith("…")
    assert title.startswith(long_text[:20])


def test_title_from_thread_messages_fallback():
    """无 user 文本时回退为默认标题。"""
    assert _title_from_thread_messages([]) == "新对话"
    assert _title_from_thread_messages([ToolMessage(content="ok", tool_call_id="c1")]) == "新对话"