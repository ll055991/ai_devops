"""审批中间件单元测试。

覆盖 DeployApprovalMiddleware + EnvScopingMiddleware：
- 触发中断（GraphInterrupt）
- approve 保留 tool_call
- reject 移除 tool_call + 追加错误 ToolMessage
- 非审批工具不触发
- 决策数量不匹配报错
- EnvScoping 注入白名单值
- 日志格式断言

测试策略：纯单元测试 middleware，不依赖真实 LLM。
用 monkeypatch mock langgraph.types.interrupt 模拟人工决策。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphInterrupt

from deploy_agent import middleware
from deploy_agent.middleware import DeployApprovalMiddleware, EnvScopingMiddleware
from deploy_agent.settings import Settings


def _make_settings() -> Settings:
    """构造测试用 settings。"""
    return Settings(
        container_names_raw="ontology-graph",
        workspaces_raw="/data/deploy/workspace",
        image_prefixes_raw="ontology/ontology-graph",
        # 指向不存在的路径，防止真实 whitelist.json 在 model_post_init 覆盖测试值
        whitelist_file="__nonexistent_whitelist_test__.json",
        server_host="10.1.248.143",
        server_port=22,
        server_user="root",
        server_password="secret",
        gitlab_user="user",
        gitlab_token="token",
        health_url="http://127.0.0.1:8080/healthz",
        approval_required_tools_raw="stop_container,start_container",
    )


@pytest.fixture
def settings() -> Settings:
    return _make_settings()


class _FakeRuntime:
    """模拟 runtime，提供 context（含 thread_id）。"""

    def __init__(self, thread_id: str = "test-thread-1"):
        self.context = {"configurable": {"thread_id": thread_id}}


def _make_state(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """构造 AgentState 字典，含一条带 tool_calls 的 AIMessage。"""
    ai_msg = AIMessage(content="", tool_calls=tool_calls)
    return {"messages": [ai_msg]}


def _make_tool_call(
    name: str, args: dict[str, Any], call_id: str = "call-1"
) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


# ==================== DeployApprovalMiddleware ====================


async def test_approval_triggers_interrupt(settings, monkeypatch):
    """stop_container tool_call 必须触发 GraphInterrupt。

    mock interrupt 抛 GraphInterrupt（真实 interrupt 需要图执行上下文）。
    """
    def fake_interrupt(value):
        raise GraphInterrupt()

    monkeypatch.setattr(middleware, "interrupt", fake_interrupt)

    mw = DeployApprovalMiddleware(settings)
    state = _make_state([
        _make_tool_call("stop_container", {"container_name": "ontology-graph"})
    ])

    with pytest.raises(GraphInterrupt):
        await mw.aafter_model(state, _FakeRuntime())


async def test_approval_non_matching_tool_passes(settings, monkeypatch):
    """非审批工具（如 check_service_health）不触发 interrupt。"""
    interrupt_called = {"v": False}

    def fake_interrupt(value):
        interrupt_called["v"] = True
        return {"decisions": []}

    monkeypatch.setattr(middleware, "interrupt", fake_interrupt)

    mw = DeployApprovalMiddleware(settings)
    state = _make_state([
        _make_tool_call("check_service_health", {"container_name": "ontology-graph"})
    ])

    # 不应触发 interrupt，返回 None
    result = await mw.aafter_model(state, _FakeRuntime())
    assert result is None
    assert interrupt_called["v"] is False


async def test_approval_approve_executes(settings, monkeypatch):
    """approve 决策保留 tool_call。"""
    def fake_interrupt(value):
        return {"decisions": [{"type": "approve"}]}

    monkeypatch.setattr(middleware, "interrupt", fake_interrupt)

    mw = DeployApprovalMiddleware(settings)
    state = _make_state([
        _make_tool_call("stop_container", {"container_name": "ontology-graph"})
    ])

    result = await mw.aafter_model(state, _FakeRuntime())

    # tool_call 保留
    assert result is not None
    ai_msg = result["messages"][0]
    assert len(ai_msg.tool_calls) == 1
    assert ai_msg.tool_calls[0]["name"] == "stop_container"


async def test_approval_reject_blocks(settings, monkeypatch):
    """reject 决策移除 tool_call + 追加错误 ToolMessage 文本。"""
    def fake_interrupt(value):
        return {"decisions": [{"type": "reject", "message": "用户拒绝停止容器"}]}

    monkeypatch.setattr(middleware, "interrupt", fake_interrupt)

    mw = DeployApprovalMiddleware(settings)
    state = _make_state([
        _make_tool_call("stop_container", {"container_name": "ontology-graph"})
    ])

    result = await mw.aafter_model(state, _FakeRuntime())

    # tool_call 被移除
    assert result is not None
    ai_msg = result["messages"][0]
    assert len(ai_msg.tool_calls) == 0
    # AI 消息追加拒绝原因
    assert "用户拒绝停止容器" in ai_msg.content


async def test_approval_decision_count_mismatch(settings, monkeypatch):
    """决策数量不匹配必须 raise ValueError。

    构造 3 个审批 tool_call，只返回 1 个决策（单条广播会处理 2 个，第 3 个仍不匹配）。
    """
    def fake_interrupt(value):
        return {"decisions": [{"type": "approve"}]}

    monkeypatch.setattr(middleware, "interrupt", fake_interrupt)

    mw = DeployApprovalMiddleware(settings)
    # 3 个审批 tool_call，单条广播后 decisions 变 3 条，无法触发不匹配
    # 改为返回 2 条决策
    def fake_interrupt2(value):
        return {"decisions": [{"type": "approve"}, {"type": "approve"}]}

    monkeypatch.setattr(middleware, "interrupt", fake_interrupt2)

    state = _make_state([
        _make_tool_call("stop_container", {"container_name": "ontology-graph"}, "call-1"),
        _make_tool_call("start_container", {"container_name": "ontology-graph", "image": "ontology/ontology-graph:v1"}, "call-2"),
        _make_tool_call("stop_container", {"container_name": "ontology-graph"}, "call-3"),
    ])

    with pytest.raises(ValueError, match="决策数量"):
        await mw.aafter_model(state, _FakeRuntime())


async def test_approval_edit_treated_as_reject(settings, monkeypatch):
    """edit 决策按 reject 处理。"""
    def fake_interrupt(value):
        return {"decisions": [{"type": "edit", "edited_action": {"name": "stop_container", "args": {}}}]}

    monkeypatch.setattr(middleware, "interrupt", fake_interrupt)

    mw = DeployApprovalMiddleware(settings)
    state = _make_state([
        _make_tool_call("stop_container", {"container_name": "ontology-graph"})
    ])

    result = await mw.aafter_model(state, _FakeRuntime())
    ai_msg = result["messages"][0]
    assert len(ai_msg.tool_calls) == 0
    assert "编辑决策暂不支持" in ai_msg.content


# ==================== EnvScopingMiddleware ====================


async def test_env_scoping_container_name_not_overridden(settings):
    """白名单改多值后，middleware 不再强制覆盖 container_name（校验下沉到 tool 内部）。

    传入的 container_name 应保持原值不变，由 tool 内部做白名单校验。
    """
    mw = EnvScopingMiddleware(settings)

    tool_call = _make_tool_call(
        "stop_container", {"container_name": "wrong-container"}
    )

    class _Req:
        def __init__(self, tc):
            self.tool_call = tc

    class _Handler:
        async def __call__(self, req):
            return "ok"

    await mw.awrap_tool_call(_Req(tool_call), _Handler())

    # middleware 不再覆盖，参数原样透传，校验由 tool 内部完成
    assert tool_call["args"]["container_name"] == "wrong-container"


async def test_env_scoping_repo_url_not_overridden(settings):
    """repo_url 无白名单后，middleware 不再覆盖（由用户在对话中指定）。"""
    mw = EnvScopingMiddleware(settings)

    tool_call = _make_tool_call(
        "git_pull_code",
        {"repo_url": "http://wrong.com/repo.git", "branch": "develop", "workspace": "/data"},
    )

    class _Req:
        def __init__(self, tc):
            self.tool_call = tc

    class _Handler:
        async def __call__(self, req):
            return "ok"

    await mw.awrap_tool_call(_Req(tool_call), _Handler())

    # middleware 不再覆盖，repo_url 原样透传
    assert tool_call["args"]["repo_url"] == "http://wrong.com/repo.git"


async def test_env_scoping_non_target_tool_unchanged(settings):
    """非目标工具的参数不应被修改。"""
    mw = EnvScopingMiddleware(settings)

    tool_call = _make_tool_call(
        "check_service_health", {"container_name": "any-container"}
    )

    class _Req:
        def __init__(self, tc):
            self.tool_call = tc

    class _Handler:
        async def __call__(self, req):
            return "ok"

    await mw.awrap_tool_call(_Req(tool_call), _Handler())

    # check_service_health 的 container_name 不被注入
    assert tool_call["args"]["container_name"] == "any-container"


# ==================== 日志断言 ====================


async def test_approval_log_messages(settings, monkeypatch):
    """断言日志文件中出现 APPROVAL | event=required 与 event=decided。

    loguru 不走标准 logging 模块，caplog 无法直接捕获，
    改为读日志文件断言（日志已配置写入 logs/deploy_agent.log）。
    """
    def fake_interrupt(value):
        return {"decisions": [{"type": "approve"}]}

    monkeypatch.setattr(middleware, "interrupt", fake_interrupt)

    mw = DeployApprovalMiddleware(settings)
    state = _make_state([
        _make_tool_call("stop_container", {"container_name": "ontology-graph"})
    ])

    # 记录执行前的日志文件行数，执行后只读新增部分
    from deploy_agent.logging import _resolve_log_dir
    log_file = _resolve_log_dir() / "deploy_agent.log"
    if log_file.exists():
        old_lines = log_file.read_text(encoding="utf-8").splitlines()
        old_count = len(old_lines)
    else:
        old_count = 0

    await mw.aafter_model(state, _FakeRuntime())

    # 读新增日志行
    new_text = log_file.read_text(encoding="utf-8")
    new_lines = new_text.splitlines()[old_count:]
    new_log = "\n".join(new_lines)

    assert "APPROVAL | event=required" in new_log
    assert "APPROVAL | event=decided" in new_log
    assert "stop_container" in new_log
    assert "thread_id=test-thread-1" in new_log
