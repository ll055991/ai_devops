"""Evals 运行器（M1）：端到端跑 Agent，收集轨迹。

唯一测试 seam：真调小模型 + SSH 命令层 mock + 审批中断 mock，
断言审计记录 + 工具调用序列 + 部署状态。不新增低层 seam。

隔离：每个场景独立 thread_id、独立 tmp 状态库/审计库、独立
whitelist_file；中间件单例经 monkeypatch 指向临时库，不碰生产库。
"""

from __future__ import annotations

import asyncio
import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from deploy_agent import middleware as middleware_module
from deploy_agent import tools as tools_module
from deploy_agent.audit import AuditLogStore
from deploy_agent.factory import create_deploy_agent
from deploy_agent.settings import Settings
from deploy_agent.state import DeploymentStateStore

from .scenarios import DEFAULT_ROUTES, Scenario

APPROVAL_TOOLS_RAW = (
    "stop_container,remove_container,start_container,"
    "write_workspace_file,delete_workspace_file,"
    "add_whitelist_entry,remove_whitelist_entry,rollback_deployment"
)


@dataclass
class Trajectory:
    """单场景轨迹：只含外部可观察行为，不断言 LLM 原文。

    calls 是原子工具调用列表（含子 Agent 内的调用），来源为审计记录
    （按写入顺序）。主图 messages 只能看到 task/主 Agent 工具，子 Agent
    的 git/build/巡检调用在嵌套图里，必须从审计还原。
    """

    scenario_id: str
    calls: list[dict[str, Any]] = field(default_factory=list)  # 按序 {name, status, args, thread_id}
    approvals: list[dict[str, Any]] = field(default_factory=list)  # interrupt 载荷
    audit_rows: list[dict[str, Any]] = field(default_factory=list)
    deployment: dict[str, Any] | None = None
    final_text: str = ""
    ssh_calls: list[str] = field(default_factory=list)


def _parse_args_summary(raw: Any) -> dict[str, Any]:
    """把审计行的 args_summary（打码参数 repr）解析回 dict；失败返回空 dict。

    只用于断言参数合法性：本项目工具参数不含密钥字段，打码不改变
    可读值（>40 字符的字符串会被截断，断言按前缀/白名单判断不受影响）。
    """
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _all_called(rows: list[dict[str, Any]], required: list[str]) -> bool:
    """审计里是否已出现全部必调工具（用于逐轮推进/提前结束）。"""
    if not required:
        return False
    called = {r.get("tool_name") for r in rows}
    return all(name in called for name in required)


def _calls_from_audit(rows_desc: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """审计行（created_at DESC, id DESC）→ 原子调用列表（按发生顺序）。"""
    calls: list[dict[str, Any]] = []
    for row in reversed(rows_desc):
        calls.append(
            {
                "name": row.get("tool_name", ""),
                "status": row.get("status", ""),
                "args": _parse_args_summary(row.get("args_summary")),
                "thread_id": row.get("thread_id"),
                "result_summary": row.get("result_summary"),
            }
        )
    return calls


class FakeSSH:
    """按命令关键字路由的 SSH 替身（沿用工具测试的 mock 传统）。

    ssh_raise 命中的命令抛 ConnectionError；routes 优先于默认路由；
    未命中返回成功空输出。stream 版本逐行回调 on_line。
    """

    def __init__(self, scenario: Scenario):
        self.routes = list(scenario.ssh_routes) + DEFAULT_ROUTES
        self.raise_on = list(scenario.ssh_raise)
        self.calls: list[str] = []

    def _match(self, command: str) -> tuple[int, str, str]:
        if any(k in command for k in self.raise_on):
            raise ConnectionError("SSH connection refused (eval fake)")
        for keyword, response in self.routes:
            if keyword in command:
                return response
        return 0, "", ""

    async def run_ssh(self, settings: Any, command: str, timeout: int = 60):
        self.calls.append(command)
        return self._match(command)

    async def run_ssh_stream(
        self,
        settings: Any,
        command: str,
        timeout: int = 600,
        on_line: Any = None,
    ):
        self.calls.append(command)
        exit_code, out, err = self._match(command)
        if on_line is not None:
            for line in out.splitlines():
                on_line(line)
        return exit_code, out, err


def _eval_settings(tmp_path: Path) -> Settings:
    return Settings(
        container_names_raw="ontology-graph",
        workspaces_raw="/data/deploy/workspace,/data/test",
        image_prefixes_raw="ontology/ontology-graph,infra/data-service",
        whitelist_file=str(tmp_path / "whitelist.json"),
        server_host="10.1.248.143",
        server_port=22,
        server_user="root",
        server_password="secret",
        gitlab_user="user",
        gitlab_token="token",
        health_url="http://127.0.0.1:8080/healthz",
        approval_required_tools_raw=APPROVAL_TOOLS_RAW,
    )


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content)


# 基线收集：[(scenario_id, model, [(指标, 通过, 说明)])]，conftest 落盘
REPORT: list[tuple[str, str, list[tuple[str, bool, str]]]] = []


async def run_scenario(
    scenario: Scenario, monkeypatch: Any, tmp_path: Path, timeout: int = 300
) -> tuple[Trajectory, Settings]:
    """跑一条场景，返回（轨迹，settings）。无 LLM 凭证时 skip（门禁在有凭证环境跑）。"""
    settings = _eval_settings(tmp_path)
    if settings.openai_api_key is None:
        pytest.skip("evals 需要 LLM 凭证（OPENAI_API_KEY）")

    dep_store = DeploymentStateStore(tmp_path / "deployments.db")
    audit_store = AuditLogStore(tmp_path / "audit.db")
    monkeypatch.setattr(
        middleware_module, "get_deployment_store", lambda: dep_store
    )
    monkeypatch.setattr(middleware_module, "get_audit_store", lambda: audit_store)

    fake = FakeSSH(scenario)
    monkeypatch.setattr(tools_module, "_run_ssh", fake.run_ssh)
    monkeypatch.setattr(tools_module, "_run_ssh_stream", fake.run_ssh_stream)

    approvals: list[dict[str, Any]] = []

    def fake_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        approvals.append(payload)
        return {"decisions": scenario.decisions}

    monkeypatch.setattr(middleware_module, "interrupt", fake_interrupt)

    seed_id = ""
    if scenario.seed:
        rec = await dep_store.record(scenario.thread_id, **scenario.seed)
        seed_id = str(rec.get("id", ""))

    agent = create_deploy_agent(settings, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": scenario.thread_id}}
    message = scenario.message.format(deployment_id=seed_id)

    try:
        traj = Trajectory(
            scenario_id=scenario.id, approvals=approvals, ssh_calls=fake.calls
        )
        # 首轮 + 追问轮：同一 thread 顺序执行，轨迹累积。
        # 强约束：必调工具没跑齐就继续追问（Agent 中途停下来征询时推它走完），
        # 跑齐即提前结束，省下多余 LLM 轮次。
        for user_text in [message, *scenario.follow_ups]:
            result = await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": [{"role": "user", "content": user_text}]}, config
                ),
                timeout,
            )
            messages = result.get("messages", []) if isinstance(result, dict) else []
            for msg in messages:
                if isinstance(msg, AIMessage):
                    text = _extract_text(msg.content)
                    if text.strip():
                        traj.final_text = text
            if scenario.required_tools:
                rows = await audit_store.list(limit=200)
                if _all_called(rows, scenario.required_tools):
                    break
        traj.audit_rows = await audit_store.list(limit=200)
        traj.calls = _calls_from_audit(traj.audit_rows)
        traj.deployment = await dep_store.get_by_thread(scenario.thread_id)
        return traj, settings
    finally:
        await dep_store.close()
        await audit_store.close()
