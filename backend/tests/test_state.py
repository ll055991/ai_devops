"""部署状态存储与中间件单元测试（改造方案阶段 1）。

覆盖：
- DeploymentStateStore：建表、record 创建/更新、get_by_thread、list_history、close
- DeploymentStateMiddleware：工具成功/失败自动更新部署状态、非跟踪工具不记录
- 新工具：check_server_environment / get_container_logs / get_deployment_status / list_deployment_history

测试策略：
- store 用 tmp_path 临时库，不碰单例（backend/checkpoints/deployments.db）
- 中间件用 mock handler 返回结构化 JSON，不依赖真实 LLM/SSH
- SSH 工具用 monkeypatch mock _run_ssh（与 test_tools.py 同一模式）
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage

from deploy_agent import tools
from deploy_agent.middleware import DeploymentStateMiddleware
from deploy_agent.settings import Settings
from deploy_agent.state import DeploymentStateStore


def _make_settings() -> Settings:
    """构造测试用 settings（不走 .env）。"""
    return Settings(
        container_names_raw="ontology-graph",
        workspaces_raw="/data/deploy/workspace,/data/test",
        image_prefixes_raw="ontology/ontology-graph,infra/data-service",
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


@pytest_asyncio.fixture
async def store(tmp_path):
    """临时目录下的 DeploymentStateStore（每个测试独立）。"""
    s = DeploymentStateStore(tmp_path / "deployments.db")
    yield s
    await s.close()


class _FakeRuntime:
    """模拟 runtime，提供 context（含 thread_id）。"""

    def __init__(self, thread_id: str = "test-thread-1"):
        self.context = {"configurable": {"thread_id": thread_id}}


def _make_request(tool_name: str, args: dict, thread_id: str = "test-thread-1"):
    """构造 ToolCallRequest（tool_call + runtime）。"""
    tool_call = {"name": tool_name, "args": args, "id": "call-1", "type": "tool_call"}
    return ToolCallRequest(
        tool_call=tool_call,
        tool=None,
        state={},
        runtime=_FakeRuntime(thread_id),
    )


def _tool_message(payload: dict) -> ToolMessage:
    """按链内真实形状包装工具结果：handler 返回 ToolMessage，不是原生 str。"""
    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        name="mock-tool",
        tool_call_id="call-1",
    )


class _OkHandler:
    """mock 工具 handler：返回 success=true 的结构化 JSON（ToolMessage 形态）。"""

    def __init__(self, payload: dict):
        self.payload = payload

    async def __call__(self, request):
        return _tool_message({"success": True, **self.payload})


class _FailHandler:
    """mock 工具 handler：返回 success=false 的结构化 JSON（ToolMessage 形态）。"""

    async def __call__(self, request):
        return _tool_message(
            {"success": False, "error_type": "command_failed", "message": "docker stop 失败"}
        )


# ==================== DeploymentStateStore ====================


async def test_store_record_creates_pending(store):
    """首次 record 创建 pending 记录，且 created_at/updated_at 有值。"""
    rec = await store.record("t-1", commit="a81f92c")
    assert rec["thread_id"] == "t-1"
    assert rec["status"] == "pending"
    assert rec["commit"] == "a81f92c"
    assert rec["created_at"] and rec["updated_at"]


async def test_store_record_updates_existing(store):
    """二次 record 更新非 None 字段，不覆盖旧值。"""
    await store.record("t-1", commit="a81f92c")
    rec = await store.record("t-1", image="my-app:v1", status="building")
    assert rec["commit"] == "a81f92c"  # 旧字段保留
    assert rec["image"] == "my-app:v1"
    assert rec["status"] == "building"


async def test_store_record_ignores_unknown_field(store):
    """非法字段（不在白名单内）被忽略且不报错。"""
    rec = await store.record("t-1", hack="x")
    assert rec["status"] == "pending"
    assert "hack" not in rec


async def test_store_get_by_thread_none(store):
    """不存在的 thread_id 返回 None。"""
    assert await store.get_by_thread("t-ghost") is None


async def test_store_list_history_order_and_filter(store):
    """list_history 按时间倒序 + 容器过滤 + limit 生效。"""
    await store.record("t-1", container="app-a", image="a:v1")
    await store.record("t-2", container="app-b", image="b:v1")
    await store.record("t-3", container="app-a", image="a:v2")

    all_history = await store.list_history()
    assert len(all_history) == 3
    # 最新在前：t-3 最先
    assert all_history[0]["thread_id"] == "t-3"

    filtered = await store.list_history(container="app-a")
    assert len(filtered) == 2
    assert {r["container"] for r in filtered} == {"app-a"}

    limited = await store.list_history(limit=1)
    assert len(limited) == 1


async def test_store_close_idempotent(store):
    """close 幂等，可重复调用。"""
    await store.close()
    await store.close()


# ==================== DeploymentStateMiddleware ====================


async def test_middleware_tracks_build_success(settings, store):
    """build_docker_image 成功后 status=building + image 写入。"""
    mw = DeploymentStateMiddleware(settings, store=store)
    handler = _OkHandler({"image": "ontology/ontology-graph:v1", "log": "..."})
    result = await mw.awrap_tool_call(
        _make_request(
            "build_docker_image",
            {"code_path": "/data/test", "image_name": "ontology/ontology-graph", "image_tag": "v1"},
        ),
        handler,
    )
    # 工具结果原样返回（链内形态 ToolMessage）
    assert json.loads(result.content)["success"] is True
    rec = await store.get_by_thread("test-thread-1")
    assert rec["status"] == "building"
    assert rec["image"] == "ontology/ontology-graph:v1"


async def test_middleware_tracks_git_pull_fields(settings, store):
    """git_pull_code 成功写入 commit/branch/environment（workspace），状态保持 pending。"""
    mw = DeploymentStateMiddleware(settings, store=store)
    handler = _OkHandler({"branch": "develop", "commit": "a81f92c"})
    await mw.awrap_tool_call(
        _make_request(
            "git_pull_code",
            {"repo_url": "http://example.com/repo.git", "branch": "develop", "workspace": "/data/test"},
        ),
        handler,
    )
    rec = await store.get_by_thread("test-thread-1")
    assert rec["commit"] == "a81f92c"
    assert rec["branch"] == "develop"
    assert rec["environment"] == "/data/test"
    assert rec["status"] == "pending"


async def test_middleware_tracks_health_status(settings, store):
    """check_service_health 按结果写 healthy / unhealthy。"""
    mw = DeploymentStateMiddleware(settings, store=store)
    await mw.awrap_tool_call(
        _make_request("check_service_health", {"container_name": "ontology-graph"}),
        _OkHandler({"status": "healthy", "container_status": "running", "http_status": "200"}),
    )
    assert (await store.get_by_thread("test-thread-1"))["status"] == "healthy"

    await mw.awrap_tool_call(
        _make_request("check_service_health", {"container_name": "ontology-graph"}),
        _OkHandler({"status": "unhealthy", "container_status": "running", "http_status": "500"}),
    )
    assert (await store.get_by_thread("test-thread-1"))["status"] == "unhealthy"


async def test_middleware_tracks_failure(settings, store):
    """工具失败（success=false）时 status=failed + error 记录。"""
    mw = DeploymentStateMiddleware(settings, store=store)
    await mw.awrap_tool_call(
        _make_request("stop_container", {"container_name": "ontology-graph"}),
        _FailHandler(),
    )
    rec = await store.get_by_thread("test-thread-1")
    assert rec["status"] == "failed"
    assert "command_failed" in rec["error"]


async def test_middleware_ignores_untracked_tool(settings, store):
    """非跟踪工具（list_containers）不创建部署记录。"""
    mw = DeploymentStateMiddleware(settings, store=store)
    await mw.awrap_tool_call(
        _make_request("list_containers", {}),
        _OkHandler({"containers": [], "count": 0}),
    )
    assert await store.get_by_thread("test-thread-1") is None


async def test_middleware_tracks_via_execution_info(settings, store):
    """生产形状：thread_id 从 runtime.execution_info 取（context 是 RuntimeContext，非 dict）。"""

    class _Info:
        thread_id = "t-exec"

    class _Runtime:
        execution_info = _Info()
        context = object()

    request = ToolCallRequest(
        tool_call={
            "name": "build_docker_image",
            "args": {"code_path": "/data/test"},
            "id": "call-1",
            "type": "tool_call",
        },
        tool=None,
        state={},
        runtime=_Runtime(),
    )
    mw = DeploymentStateMiddleware(settings, store=store)
    await mw.awrap_tool_call(request, _OkHandler({"image": "x:v1"}))

    rec = await store.get_by_thread("t-exec")
    assert rec is not None and rec["image"] == "x:v1"


async def test_thread_id_falls_back_to_context_var(settings, store):
    """嵌套子图场景：execution_info/context 都拿不到时，用 ContextVar 里的父会话 thread_id。

    回归：deepagents 调用子 Agent 不透传 config，审计/状态记录会成孤儿。
    """
    from deploy_agent.middleware import _thread_id_ctx

    class _SubagentRuntime:
        """子 Agent 运行时形状：execution_info 无 thread_id，context 非 dict。"""

        class _Info:
            thread_id = None

        execution_info = _Info()
        context = object()

    token = _thread_id_ctx.set("t-parent")
    try:
        request = ToolCallRequest(
            tool_call={
                "name": "build_docker_image",
                "args": {"code_path": "/data/test"},
                "id": "call-1",
                "type": "tool_call",
            },
            tool=None,
            state={},
            runtime=_SubagentRuntime(),
        )
        mw = DeploymentStateMiddleware(settings, store=store)
        await mw.awrap_tool_call(request, _OkHandler({"image": "x:v1"}))
        rec = await store.get_by_thread("t-parent")
        assert rec is not None and rec["image"] == "x:v1"
    finally:
        _thread_id_ctx.reset(token)


async def test_middleware_skips_without_thread_id(settings, store):
    """拿不到 thread_id 时跳过跟踪，不报错。"""
    mw = DeploymentStateMiddleware(settings, store=store)
    result = await mw.awrap_tool_call(
        _make_request("build_docker_image", {"code_path": "/data/test"}, thread_id=""),
        _OkHandler({"image": "x:v1"}),
    )
    assert json.loads(result.content)["success"] is True
    assert await store.get_by_thread("") is None


# ==================== check_server_environment ====================


async def test_check_server_environment_success(settings, monkeypatch):
    """SSH 返回分段巡检输出时正确解析 docker/git/disk/memory。"""

    async def fake_run_ssh(s, command, timeout=60):
        return (
            0,
            "===DOCKER===\n"
            "Docker version 26.1.3, build b72abbb\n"
            "===GIT===\n"
            "git version 2.39.2\n"
            "===DISK===\n"
            "/dev/vda1|100G|62G|34G|65%|/\n"
            "===MEM===\n"
            "15966|4098|8000|11546\n"
            "===END===\n",
            "",
        )

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_check_server_environment_tool(settings)
    result = await tool.ainvoke({})
    data = json.loads(result)
    assert data["success"] is True
    assert data["host"] == "10.1.248.143"
    assert "Docker version" in data["docker_version"]
    assert "git version" in data["git_version"]
    assert data["disk"]["use_percent"] == "65%"
    assert data["memory"]["total_mb"] == 15966
    assert data["memory"]["available_mb"] == 11546


async def test_check_server_environment_ssh_failed(settings, monkeypatch):
    """SSH 失败返回 ssh_error。"""

    async def fake_run_ssh(s, command, timeout=60):
        raise ConnectionError("SSH connection refused")

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_check_server_environment_tool(settings)
    data = json.loads(await tool.ainvoke({}))
    assert data["success"] is False
    assert data["error_type"] == "ssh_error"


# ==================== get_container_logs ====================


async def test_get_container_logs_empty_name(settings):
    """container_name 为空返回 validation_error。"""
    tool = tools.build_get_container_logs_tool(settings)
    data = json.loads(await tool.ainvoke({"container_name": ""}))
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_get_container_logs_invalid_tail(settings):
    """tail 超范围返回 validation_error。"""
    tool = tools.build_get_container_logs_tool(settings)
    data = json.loads(
        await tool.ainvoke({"container_name": "ontology-graph", "tail": 99999})
    )
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_get_container_logs_success(settings, monkeypatch):
    """成功路径：命令含 --tail 与 --timestamps，行数统计正确。"""
    seen_cmds: list[str] = []

    async def fake_run_ssh(s, command, timeout=60):
        seen_cmds.append(command)
        return 0, "2026-08-20T10:30:01Z INFO started\n2026-08-20T10:30:02Z ERROR boom\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_get_container_logs_tool(settings)
    data = json.loads(
        await tool.ainvoke({"container_name": "ontology-graph", "tail": 200})
    )
    assert data["success"] is True
    assert data["count"] == 2
    assert data["lines"][1] == "2026-08-20T10:30:02Z ERROR boom"
    assert data["truncated"] is False
    assert "--tail 200" in seen_cmds[0]
    assert "--timestamps" in seen_cmds[0]
    assert "grep" not in seen_cmds[0]


async def test_get_container_logs_with_keyword(settings, monkeypatch):
    """keyword 非空时命令含 grep -F，且容器名被 shlex.quote 防注入。"""
    seen_cmds: list[str] = []

    async def fake_run_ssh(s, command, timeout=60):
        seen_cmds.append(command)
        return 0, "2026-08-20T10:30:02Z ERROR boom\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_get_container_logs_tool(settings)
    data = json.loads(
        await tool.ainvoke(
            {"container_name": "my;rm -rf /", "tail": 100, "keyword": "ERROR"}
        )
    )
    assert data["success"] is True
    assert data["count"] == 1
    # 注入容器名被引号包裹，不直接拼进命令
    assert "'my;rm -rf /'" in seen_cmds[0]
    assert "grep -F -- ERROR" in seen_cmds[0]
    assert "|| true" in seen_cmds[0]


async def test_get_container_logs_truncated(settings, monkeypatch):
    """超过 500 行时截断到后 500 行 + truncated=true。"""
    lines = "\n".join(f"line-{i}" for i in range(600))

    async def fake_run_ssh(s, command, timeout=60):
        return 0, lines + "\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_get_container_logs_tool(settings)
    data = json.loads(await tool.ainvoke({"container_name": "ontology-graph"}))
    assert data["success"] is True
    assert data["truncated"] is True
    assert data["count"] == 500
    # 保留最新（末尾）500 行
    assert data["lines"][0] == "line-100"


async def test_get_container_logs_command_failed(settings, monkeypatch):
    """docker logs 失败（exit!=0）返回 command_failed。"""

    async def fake_run_ssh(s, command, timeout=60):
        return 1, "", "Error: No such container"

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_get_container_logs_tool(settings)
    data = json.loads(await tool.ainvoke({"container_name": "ghost"}))
    assert data["success"] is False
    assert data["error_type"] == "command_failed"


# ==================== get_deployment_status / list_deployment_history ====================


async def test_get_deployment_status_empty_thread_id(settings):
    """thread_id 为空返回 validation_error。"""
    tool = tools.build_get_deployment_status_tool(settings)
    data = json.loads(await tool.ainvoke({"thread_id": ""}))
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_get_deployment_status_not_found(settings, tmp_path):
    """查询不到记录：success=true + found=false（正常结果，非错误）。"""
    store = DeploymentStateStore(tmp_path / "d.db")
    tool = tools.build_get_deployment_status_tool(settings, store=store)
    data = json.loads(await tool.ainvoke({"thread_id": "t-ghost"}))
    assert data["success"] is True
    assert data["found"] is False
    assert data["deployment"] is None
    await store.close()


async def test_get_deployment_status_found(settings, tmp_path):
    """查询到记录：返回完整 deployment。"""
    store = DeploymentStateStore(tmp_path / "d.db")
    await store.record("t-1", commit="a81f92c", status="building", image="x:v1")
    tool = tools.build_get_deployment_status_tool(settings, store=store)
    data = json.loads(await tool.ainvoke({"thread_id": "t-1"}))
    assert data["success"] is True
    assert data["found"] is True
    assert data["deployment"]["status"] == "building"
    assert data["deployment"]["commit"] == "a81f92c"
    await store.close()


async def test_list_deployment_history_invalid_limit(settings, tmp_path):
    """limit 超范围返回 validation_error。"""
    store = DeploymentStateStore(tmp_path / "d.db")
    tool = tools.build_list_deployment_history_tool(settings, store=store)
    data = json.loads(await tool.ainvoke({"limit": 0}))
    assert data["success"] is False
    assert data["error_type"] == "validation_error"
    await store.close()


async def test_list_deployment_history_success(settings, tmp_path):
    """成功路径：返回倒序历史 + count。"""
    store = DeploymentStateStore(tmp_path / "d.db")
    await store.record("t-1", container="app-a", image="a:v1")
    await store.record("t-2", container="app-a", image="a:v2")
    tool = tools.build_list_deployment_history_tool(settings, store=store)
    data = json.loads(await tool.ainvoke({"container": "app-a", "limit": 10}))
    assert data["success"] is True
    assert data["count"] == 2
    assert data["history"][0]["image"] == "a:v2"  # 最新在前
    await store.close()