"""审计中间件单元测试。

覆盖 AuditLogMiddleware：
- 工具成功 → ok；工具失败 → failed
- 风险控制拒绝（risk_blocked / concurrency_conflict）→ blocked
- 与 RiskControlMiddleware 串联时审计状态正确

测试策略：注入 tmp_path 临时审计库 + 部署状态库，不碰单例、不依赖 SSH/LLM。
"""

from __future__ import annotations

import json

import httpx
import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage

from deploy_agent import api
from deploy_agent.audit import AuditLogStore
from deploy_agent.middleware import AuditLogMiddleware, RiskControlMiddleware
from deploy_agent.settings import Settings
from deploy_agent.state import DeploymentStateStore


def _make_settings() -> Settings:
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
    )


@pytest.fixture
def settings() -> Settings:
    return _make_settings()


@pytest.fixture
async def audit_store(tmp_path):
    store = AuditLogStore(tmp_path / "audit.db")
    yield store
    await store.close()


@pytest.fixture
async def dep_store(tmp_path):
    store = DeploymentStateStore(tmp_path / "deployments.db")
    yield store
    await store.close()


class _FakeRuntime:
    def __init__(self, thread_id: str = "t-audit"):
        self.context = {"configurable": {"thread_id": thread_id}}


def _make_request(tool_name: str, args: dict) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": tool_name, "args": args, "id": "call-1", "type": "tool_call"},
        tool=None,
        state={},
        runtime=_FakeRuntime(),
    )


def _tool_message(payload: dict) -> ToolMessage:
    """按链内真实形状包装：handler 返回 ToolMessage，不是原生 str。"""
    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        name="mock-tool",
        tool_call_id="call-1",
    )


async def _handler(_request):
    return _tool_message({"success": True})


async def test_audit_records_ok(settings, audit_store):
    """工具成功记 ok。"""
    mw = AuditLogMiddleware(settings, store=audit_store)
    await mw.awrap_tool_call(_make_request("list_containers", {}), _handler)

    rows = await audit_store.list()
    assert rows[0]["status"] == "ok"
    assert rows[0]["thread_id"] == "t-audit"


async def test_audit_records_failed(settings, audit_store):
    """工具返回 success=false 记 failed。"""

    async def fail_handler(_request):
        return _tool_message(
            {"success": False, "error_type": "command_failed", "message": "docker stop 失败"}
        )

    mw = AuditLogMiddleware(settings, store=audit_store)
    await mw.awrap_tool_call(_make_request("stop_container", {"container_name": "ontology-graph"}), fail_handler)

    rows = await audit_store.list()
    assert rows[0]["status"] == "failed"
    assert "command_failed" in rows[0]["result_summary"]


async def test_list_audit_route(monkeypatch, audit_store):
    """GET /api/agent/audit 返回审计记录，支持 thread_id 过滤。"""
    await audit_store.record("stop_container", thread_id="t-1", status="rejected")
    await audit_store.record("list_containers", thread_id="t-2", status="ok")
    monkeypatch.setattr(api, "get_audit_store", lambda: audit_store)

    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/agent/audit", params={"thread_id": "t-1"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["count"] == 1
    assert body["records"][0]["status"] == "rejected"


async def test_audit_records_risk_block_as_blocked(settings, audit_store, dep_store):
    """RiskControl 拒绝（remove healthy 容器）→ 审计状态 blocked。"""
    await dep_store.record("t-audit", container="ontology-graph", status="healthy")
    risk = RiskControlMiddleware(settings, store=dep_store)
    mw = AuditLogMiddleware(settings, store=audit_store)

    result = await mw.awrap_tool_call(
        _make_request("remove_container", {"container_name": "ontology-graph"}),
        lambda request: risk.awrap_tool_call(request, _handler),
    )

    assert json.loads(result)["error_type"] == "risk_blocked"
    rows = await audit_store.list()
    assert rows[0]["status"] == "blocked"
    assert rows[0]["risk_level"] == "high"
    assert "risk_blocked" in rows[0]["result_summary"]


# ==================== 虚拟文件系统路径守门（read_file 陷阱） ====================


async def test_risk_blocks_virtual_fs_path_outside_mount(settings, dep_store):
    """read_file 读服务器路径必须被拦下，并指引 read_workspace_file（不执行工具）。"""
    risk = RiskControlMiddleware(settings, store=dep_store)
    called = {"v": False}

    async def handler(_request):
        called["v"] = True
        return _tool_message({"success": True})

    result = await risk.awrap_tool_call(
        _make_request("read_file", {"file_path": "/data/deploy/workspace/Dockerfile"}),
        handler,
    )

    data = json.loads(result)
    assert data["error_type"] == "virtual_path_denied"
    assert "read_workspace_file" in data["message"]
    assert called["v"] is False


async def test_risk_escalates_repeated_virtual_path_denial(settings, dep_store):
    """同一越界调用第二次 → 升级 repeated_failure（切断死循环）。"""
    risk = RiskControlMiddleware(settings, store=dep_store)

    async def handler(_request):
        return _tool_message({"success": True})

    request = _make_request("read_file", {"file_path": "/data/deploy/workspace/Dockerfile"})
    first = json.loads(await risk.awrap_tool_call(request, handler))
    second = json.loads(await risk.awrap_tool_call(request, handler))

    assert first["error_type"] == "virtual_path_denied"
    assert second["error_type"] == "repeated_failure"


async def test_risk_allows_virtual_fs_path_in_mount(settings, dep_store):
    """read_file 读 /skills/ 挂载点、以及 ls 列虚拟根，必须放行。"""
    risk = RiskControlMiddleware(settings, store=dep_store)
    seen: list[str] = []

    async def handler(request):
        seen.append(request.tool_call["name"])
        return _tool_message({"success": True})

    await risk.awrap_tool_call(
        _make_request("read_file", {"file_path": "/skills/deployment/SKILL.md"}), handler
    )
    await risk.awrap_tool_call(_make_request("ls", {"path": "/"}), handler)
    await risk.awrap_tool_call(_make_request("ls", {}), handler)

    assert seen == ["read_file", "ls", "ls"]
