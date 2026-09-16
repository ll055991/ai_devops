"""模型构建单元测试。

覆盖 build_chat_model：
- max_tokens 从 settings.model_max_tokens 读取（.env 的 MODEL_MAX_TOKENS 生效）
- 不发起任何网络请求，只检查构造参数。
"""

from __future__ import annotations

from deploy_agent.factory import build_chat_model, build_subagents
from deploy_agent.settings import Settings
from deploy_agent.tools import build_tools


def _make_settings(**overrides) -> Settings:
    return Settings(
        container_names_raw="ontology-graph",
        workspaces_raw="/data/deploy/workspace",
        image_prefixes_raw="ontology/ontology-graph",
        whitelist_file="__nonexistent_whitelist_test__.json",
        server_host="10.1.248.143",
        server_port=22,
        server_user="root",
        server_password="secret",
        gitlab_user="user",
        gitlab_token="token",
        health_url="http://127.0.0.1:8080/healthz",
        openai_api_key="sk-test",
        **overrides,
    )


def test_build_chat_model_uses_settings_max_tokens():
    """MODEL_MAX_TOKENS 配置必须传到 ChatOpenAI（曾被硬编码 8192 忽略）。"""
    model = build_chat_model(_make_settings(model_max_tokens=32768))
    assert model.max_tokens == 32768

    # 缺省值与 settings 对象一致（不耦合真实 .env 内容）
    default_settings = _make_settings()
    assert (
        build_chat_model(default_settings).max_tokens == default_settings.model_max_tokens
    )


def test_build_subagents_returns_four_agents():
    """build_subagents 返回 code/build/deploy-planner/monitor 四个子 Agent。"""
    settings = _make_settings()
    subagents = build_subagents(build_tools(settings), settings)
    assert [s["name"] for s in subagents] == [
        "code-agent",
        "build-agent",
        "deploy-planner",
        "monitor-agent",
    ]
    for s in subagents:
        assert s["description"] and s["system_prompt"] and s["tools"]


def test_subagents_hold_no_approval_tools():
    """高风险上收：审批名单内工具不得进任何子 Agent（ADR-0005）。"""
    settings = _make_settings()
    blocked = set(settings.approval_tool_names())
    assert blocked, "测试前置：审批名单不应为空"
    for s in build_subagents(build_tools(settings), settings):
        held = {t.name for t in s["tools"]}
        assert held.isdisjoint(blocked), f"{s['name']} 持有高风险工具：{held & blocked}"


def test_subagents_carry_guard_middleware():
    """子 Agent 必须显式挂载审计/风控/状态中间件，否则调用不落审计与状态库。

    回归：deepagents 子 Agent 是独立嵌套图，主 Agent 中间件不会自动生效。
    """
    from deploy_agent.middleware import (
        AuditLogMiddleware,
        DeployApprovalMiddleware,
        DeploymentStateMiddleware,
        RiskControlMiddleware,
    )

    settings = _make_settings()
    for s in build_subagents(build_tools(settings), settings):
        kinds = {type(m) for m in s.get("middleware", [])}
        assert AuditLogMiddleware in kinds, f"{s['name']} 缺 AuditLogMiddleware"
        assert RiskControlMiddleware in kinds, f"{s['name']} 缺 RiskControlMiddleware"
        assert DeploymentStateMiddleware in kinds, f"{s['name']} 缺 DeploymentStateMiddleware"
        # 嵌套图无 checkpointer/interrupt 恢复链路，不能挂审批中间件
        assert DeployApprovalMiddleware not in kinds, f"{s['name']} 不应挂审批中间件"


def test_create_deploy_agent_wires_subagents():
    """create_deploy_agent 装配子 Agent 不报错（内存检查点，不碰真实落盘库）。

    回归：subagents 参数漏传/工具分区 KeyError 会在此暴露。
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from deploy_agent.factory import create_deploy_agent

    agent = create_deploy_agent(settings=_make_settings(), checkpointer=InMemorySaver())
    assert agent is not None
