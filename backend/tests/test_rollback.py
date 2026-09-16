"""回滚部署工具与中间件落库测试（改造方案阶段 2）。

覆盖：
- DeploymentStateStore.get_by_id：存在/不存在
- rollback_deployment 工具：
  - 参数校验（deployment_id 非法、container_name 空、容器名不在白名单）
  - 记录不存在（not_found）、记录无镜像（validation_error）
  - 成功路径（mock _run_ssh：stop → rm -f → run -d 一条命令链）
  - container_name 参数覆盖历史记录容器名
  - SSH 异常（ssh_error）、命令失败（command_failed）
- DeploymentStateMiddleware：rollback 成功后落库 status=rolled_back + rollback_from + image

store 用 pytest_asyncio.fixture 统一管理：即使断言失败 teardown 也会 close
aiosqlite 连接，避免 worker 线程未退出导致 pytest 进程挂住。
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from langchain.agents.middleware import ToolCallRequest

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
    """临时目录下的 DeploymentStateStore（每个测试独立，teardown 强制关闭连接）。"""
    s = DeploymentStateStore(tmp_path / "deployments.db")
    yield s
    await s.close()


async def _seed(store: DeploymentStateStore, thread_id: str = "t-old") -> dict:
    """预置一条带 image/container 的历史部署记录，返回完整记录 dict。"""
    return await store.record(
        thread_id,
        container="ontology-graph",
        image="ontology/ontology-graph:v1",
        commit="a81f92c",
        status="healthy",
    )


# ==================== DeploymentStateStore.get_by_id ====================


async def test_get_by_id_found(store):
    """get_by_id 能查到已存在的记录。"""
    rec = await store.record("t-1", container="app", image="a:v1")
    found = await store.get_by_id(rec["id"])
    assert found is not None
    assert found["id"] == rec["id"]
    assert found["image"] == "a:v1"


async def test_get_by_id_not_found(store):
    """get_by_id 查不到时返回 None。"""
    assert await store.get_by_id(999) is None


# ==================== rollback_deployment 参数校验 ====================


async def test_rollback_invalid_deployment_id(settings, store):
    """deployment_id 非正整数返回 validation_error。"""
    tool = tools.build_rollback_deployment_tool(settings, store=store)
    data = json.loads(await tool.ainvoke({"deployment_id": 0}))
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_rollback_empty_container_name(settings, store):
    """container_name 提供但为空返回 validation_error。"""
    await _seed(store)
    tool = tools.build_rollback_deployment_tool(settings, store=store)
    data = json.loads(await tool.ainvoke({"deployment_id": 1, "container_name": "  "}))
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_rollback_record_not_found(settings, store):
    """部署记录不存在返回 not_found。"""
    tool = tools.build_rollback_deployment_tool(settings, store=store)
    data = json.loads(await tool.ainvoke({"deployment_id": 99}))
    assert data["success"] is False
    assert data["error_type"] == "not_found"


async def test_rollback_record_without_image(settings, store):
    """记录无镜像信息无法回滚（validation_error）。"""
    rec = await store.record("t-noimg", container="ontology-graph")
    tool = tools.build_rollback_deployment_tool(settings, store=store)
    data = json.loads(await tool.ainvoke({"deployment_id": rec["id"]}))
    assert data["success"] is False
    assert data["error_type"] == "validation_error"
    assert "没有镜像" in data["message"]


async def test_rollback_container_not_in_whitelist(settings, store):
    """目标容器名不在白名单返回 validation_error（防注入 + 防操作未授权容器）。"""
    rec = await _seed(store)
    tool = tools.build_rollback_deployment_tool(settings, store=store)
    data = json.loads(
        await tool.ainvoke({"deployment_id": rec["id"], "container_name": "hacked"})
    )
    assert data["success"] is False
    assert data["error_type"] == "validation_error"
    assert "白名单" in data["message"]


# ==================== rollback_deployment 执行路径 ====================


async def test_rollback_success(settings, store, monkeypatch):
    """成功路径：一条命令链含 stop（容错）→ rm -f → run -d，返回 container/image/rollback_from。"""
    rec = await _seed(store)
    seen_cmds: list[str] = []

    async def fake_run_ssh(s, command, timeout=60):
        seen_cmds.append(command)
        return 0, "container-id-456\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_rollback_deployment_tool(settings, store=store)
    data = json.loads(await tool.ainvoke({"deployment_id": rec["id"]}))
    assert data["success"] is True
    assert data["container"] == "ontology-graph"
    assert data["image"] == "ontology/ontology-graph:v1"
    assert data["rollback_from"] == rec["id"]
    # 命令链：stop 容错 + rm -f + run -d
    assert "docker stop" in seen_cmds[0]
    assert "|| true" in seen_cmds[0]
    assert "docker rm -f" in seen_cmds[0]
    assert "docker run -d --name" in seen_cmds[0]
    assert "ontology/ontology-graph:v1" in seen_cmds[0]


async def test_rollback_success_with_container_override(settings, store, monkeypatch):
    """container_name 参数覆盖历史记录中的容器名。"""
    # 历史记录容器名不在白名单（old-app），用参数指定白名单内的容器名
    rec = await store.record("t-old", container="old-app", image="infra/data-service:v9")
    seen_cmds: list[str] = []

    async def fake_run_ssh(s, command, timeout=60):
        seen_cmds.append(command)
        return 0, "cid\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_rollback_deployment_tool(settings, store=store)
    data = json.loads(
        await tool.ainvoke({"deployment_id": rec["id"], "container_name": "ontology-graph"})
    )
    assert data["success"] is True
    assert data["container"] == "ontology-graph"
    assert data["image"] == "infra/data-service:v9"
    assert "docker run -d --name ontology-graph" in seen_cmds[0]


async def test_rollback_command_failed(settings, store, monkeypatch):
    """docker rm 或 run 失败返回 command_failed。"""
    rec = await _seed(store)

    async def fake_run_ssh(s, command, timeout=60):
        return 1, "", "docker: Error response from daemon: Conflict"

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_rollback_deployment_tool(settings, store=store)
    data = json.loads(await tool.ainvoke({"deployment_id": rec["id"]}))
    assert data["success"] is False
    assert data["error_type"] == "command_failed"
    assert "Conflict" in data["stderr"]


async def test_rollback_ssh_exception(settings, store, monkeypatch):
    """SSH 异常返回 ssh_error。"""
    rec = await _seed(store)

    async def fake_run_ssh(s, command, timeout=60):
        raise ConnectionError("SSH connection refused")

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_rollback_deployment_tool(settings, store=store)
    data = json.loads(await tool.ainvoke({"deployment_id": rec["id"]}))
    assert data["success"] is False
    assert data["error_type"] == "ssh_error"


# ==================== DeploymentStateMiddleware 落库 ====================


def _make_rollback_request(thread_id: str, deployment_id: int) -> ToolCallRequest:
    """构造 rollback_deployment 的 ToolCallRequest（带 runtime thread_id）。"""

    class _FakeRuntime:
        def __init__(self):
            self.context = {"configurable": {"thread_id": thread_id}}

    return ToolCallRequest(
        tool_call={
            "name": "rollback_deployment",
            "args": {"deployment_id": deployment_id},
            "id": "call-1",
            "type": "tool_call",
        },
        tool=None,
        state={},
        runtime=_FakeRuntime(),
    )


async def test_middleware_rollback_success(settings, store):
    """rollback 成功后：status=rolled_back + rollback_from + image 落库。"""
    old = await _seed(store)
    mw = DeploymentStateMiddleware(settings, store=store)

    class _Handler:
        async def __call__(self, req):
            return json.dumps(
                {
                    "success": True,
                    "container": "ontology-graph",
                    "image": "ontology/ontology-graph:v1",
                    "rollback_from": old["id"],
                }
            )

    await mw.awrap_tool_call(_make_rollback_request("t-rollback", old["id"]), _Handler())
    rec = await store.get_by_thread("t-rollback")
    assert rec["status"] == "rolled_back"
    assert rec["rollback_from"] == old["id"]
    assert rec["image"] == "ontology/ontology-graph:v1"
    assert rec["container"] == "ontology-graph"


async def test_middleware_rollback_failed(settings, store):
    """rollback 失败（success=false）：status=failed + error 记录。"""
    old = await _seed(store)
    mw = DeploymentStateMiddleware(settings, store=store)

    class _Handler:
        async def __call__(self, req):
            return json.dumps(
                {
                    "success": False,
                    "error_type": "command_failed",
                    "message": "回滚执行失败",
                }
            )

    await mw.awrap_tool_call(
        _make_rollback_request("t-rollback-fail", old["id"]), _Handler()
    )
    rec = await store.get_by_thread("t-rollback-fail")
    assert rec["status"] == "failed"
    assert "command_failed" in rec["error"]