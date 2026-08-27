"""工具单元测试。

覆盖：
- 参数校验分支：错误 repo_url / container_name / image 前缀必须返回失败 JSON
- SSH 执行分支：monkeypatch _run_ssh 模拟成功/失败两条路径
"""

from __future__ import annotations

import asyncio
import json

import pytest

from deploy_agent import tools
from deploy_agent.settings import Settings


def _make_settings(whitelist_file: str = "__nonexistent_whitelist_test__.json") -> Settings:
    """构造测试用 settings（直接构造，不走 .env）。

    仓库地址/分支由用户在对话中指定，不再进 settings；
    container_names 和 workspaces 是多值白名单（逗号分隔）。
    whitelist_file 指向不存在的路径：防止真实运行产生的 backend/whitelist.json
    在 model_post_init 中覆盖测试构造的白名单值。
    """
    return Settings(
        container_names_raw="ontology-graph",
        workspaces_raw="/data/deploy/workspace,/data/test",
        image_prefixes_raw="ontology/ontology-graph,infra/data-service",
        whitelist_file=whitelist_file,
        server_host="10.1.248.143",
        server_port=22,
        server_user="root",
        server_password="secret",  # SecretStr 自动转换
        gitlab_user="user",
        gitlab_token="token",
        health_url="http://127.0.0.1:8080/healthz",
    )


@pytest.fixture
def settings() -> Settings:
    return _make_settings()


# ==================== 参数校验分支（不走 SSH）====================


def test_build_repo_url_with_auth_encodes_special_chars():
    """鉴权注入必须 URL 编码 user/token，防止 @/空格 破坏 URL 结构。

    回归：token 含 `@`（如 LLy@8752792005）、user 含前导空格时，
    旧实现直接拼接导致 git 把 token 片段误当 host（Could not resolve host）。
    """
    settings = Settings(
        container_names_raw="ontology-graph",
        workspaces_raw="/data/deploy/workspace",
        image_prefixes_raw="ontology/ontology-graph",
        server_host="h",
        server_user="root",
        gitlab_user=" liuly5",
        gitlab_token="LLy@8752792005",
    )
    url = tools._build_repo_url_with_auth(
        "http://10.19.79.176:8190/DataSupplyPlatform/repo.git", settings
    )
    # 前导空格被 strip，@ -> %40
    assert url == (
        "http://liuly5:LLy%408752792005"
        "@10.19.79.176:8190/DataSupplyPlatform/repo.git"
    )


def test_build_repo_url_with_auth_keeps_original_without_creds():
    """未配置账号时保持原 URL 不变。"""
    settings = Settings(
        container_names_raw="ontology-graph",
        workspaces_raw="/data/deploy/workspace",
        image_prefixes_raw="ontology/ontology-graph",
        server_host="h",
        server_user="root",
        gitlab_user="",
        gitlab_token="",
    )
    url = tools._build_repo_url_with_auth(
        "http://10.19.79.176:8190/DataSupplyPlatform/repo.git", settings
    )
    assert url == "http://10.19.79.176:8190/DataSupplyPlatform/repo.git"


async def test_git_pull_code_wrong_workspace(settings):
    """workspace 不在白名单内必须返回失败 JSON。"""
    git_tool = tools.build_git_pull_code_tool(settings)
    result = await git_tool.ainvoke(
        {
            "repo_url": "http://example.com/test/repo.git",
            "branch": "develop",
            "workspace": "/data/wrong-not-in-whitelist",
        }
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_stop_container_wrong_name(settings):
    """错误的 container_name 必须返回失败 JSON。"""
    stop_tool = tools.build_stop_container_tool(settings)
    result = await stop_tool.ainvoke({"container_name": "wrong-container"})
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_build_docker_image_wrong_prefix(settings):
    """错误的 image_name 前缀必须返回失败 JSON。"""
    build_tool = tools.build_docker_image_tool(settings)
    result = await build_tool.ainvoke(
        {
            "code_path": "/data/test",
            "image_name": "wrong/image",
            "image_tag": "v1",
        }
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_start_container_wrong_image_prefix(settings):
    """错误的 image 前缀必须返回失败 JSON。"""
    start_tool = tools.build_start_container_tool(settings)
    result = await start_tool.ainvoke(
        {
            "container_name": "ontology-graph",
            "image": "wrong/image:v1",
        }
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_start_container_wrong_container_name(settings):
    """错误的 container_name 必须返回失败 JSON。"""
    start_tool = tools.build_start_container_tool(settings)
    result = await start_tool.ainvoke(
        {
            "container_name": "wrong-container",
            "image": "ontology/ontology-graph:v1",
        }
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


# ==================== SSH 执行分支（monkeypatch _run_ssh）====================


async def test_git_pull_code_success(settings, monkeypatch):
    """git_pull_code 成功路径：模拟 clone + rev-parse 返回 commit。"""

    async def fake_run_ssh(s, command, timeout=60):
        if "EXISTS" in command or "NOTEXISTS" in command:
            return 0, "NOTEXISTS\n", ""
        if "rev-parse" in command:
            return 0, "a81f92c\n", ""
        # clone/checkout/pull
        return 0, "", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    git_tool = tools.build_git_pull_code_tool(settings)
    result = await git_tool.ainvoke(
        {
            "repo_url": "http://example.com/test/repo.git",
            "branch": "develop",
            "workspace": "/data/test",
        }
    )
    data = json.loads(result)
    assert data["success"] is True
    assert data["commit"] == "a81f92c"
    assert data["branch"] == "develop"


async def test_git_pull_code_command_failed(settings, monkeypatch):
    """git_pull_code 失败路径：模拟 git 命令失败（exit_code!=0）。"""

    async def fake_run_ssh(s, command, timeout=60):
        if "EXISTS" in command or "NOTEXISTS" in command:
            return 0, "EXISTS\n", ""
        # pull 失败
        return 1, "", "fatal: not a git repository"

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    git_tool = tools.build_git_pull_code_tool(settings)
    result = await git_tool.ainvoke(
        {
            "repo_url": "http://example.com/test/repo.git",
            "branch": "develop",
            "workspace": "/data/test",
        }
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "command_failed"


async def test_git_pull_code_ssh_exception(settings, monkeypatch):
    """git_pull_code SSH 异常路径：模拟 paramiko 连接失败。"""

    async def fake_run_ssh(s, command, timeout=60):
        raise ConnectionError("SSH connection refused")

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    git_tool = tools.build_git_pull_code_tool(settings)
    result = await git_tool.ainvoke(
        {
            "repo_url": "http://example.com/test/repo.git",
            "branch": "develop",
            "workspace": "/data/test",
        }
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "ssh_error"


async def test_git_pull_code_dir_exists_not_git_repo(settings, monkeypatch):
    """目录存在但非 git 仓库时必须走 clone 路径（方案 B 修正点）。

    场景：workspace 已被预先 mkdir 但 .git 不存在。
    方案 B 用 git rev-parse --is-inside-work-tree 判断仓库有效性，
    非 git 目录返回 NOTEXISTS，从而走 clone 分支而非 pull 分支。
    """
    clone_called = {"v": False}

    async def fake_run_ssh(s, command, timeout=60):
        # 新 check 命令含 is-inside-work-tree，模拟非 git 仓库场景
        if "is-inside-work-tree" in command:
            return 0, "NOTEXISTS\n", ""
        if "rev-parse --short HEAD" in command:
            return 0, "a81f92c\n", ""
        # 命中 clone 路径
        clone_called["v"] = True
        return 0, "", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    git_tool = tools.build_git_pull_code_tool(settings)
    result = await git_tool.ainvoke(
        {
            "repo_url": "http://example.com/test/repo.git",
            "branch": "develop",
            "workspace": "/data/test",
        }
    )
    data = json.loads(result)
    assert data["success"] is True
    assert clone_called["v"] is True


async def test_git_pull_code_pull_fallback_to_clone(settings, monkeypatch):
    """探测误判为仓库、pull 报 not a git repository 时，必须回退到首次克隆（方案 C 修正点）。

    场景：服务器 git 版本在非仓库目录下 rev-parse 输出非 "true" 却 exit 0，
    旧逻辑只看字符串 EXISTS 误判为已存在仓库 → checkout 报 not a git repository 且永不走 clone。
    新逻辑严格比对 "true"，且 pull 失败带 not a git repository 时回退 clone。
    """
    calls = []

    async def fake_run_ssh(s, command, timeout=60):
        calls.append(command)
        if "is-inside-work-tree" in command:
            return 0, "true\n", ""
        if "git clone" in command:
            return 0, "", ""
        if "rev-parse --short HEAD" in command:
            return 0, "a81f92c\n", ""
        # pull 路径：checkout 报 not a git repository
        return 128, "", "fatal: not a git repository (or any of the parent directories): .git"

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    git_tool = tools.build_git_pull_code_tool(settings)
    result = await git_tool.ainvoke(
        {
            "repo_url": "http://example.com/test/repo.git",
            "branch": "develop",
            "workspace": "/data/test",
        }
    )
    data = json.loads(result)
    assert data["success"] is True
    assert data["commit"] == "a81f92c"
    assert any("git clone" in c for c in calls)


async def test_build_docker_image_success(settings, monkeypatch):
    """build_docker_image 成功路径（Dockerfile 在根目录）。"""

    async def fake_run_ssh(s, command, timeout=60):
        # build 联动：先走 Dockerfile 检查命令
        if "echo FOUND" in command:
            return 0, "FOUND:/data/test/Dockerfile\n", ""
        return 0, "", ""

    async def fake_run_ssh_stream(s, command, timeout=600, on_line=None):
        if on_line:
            for line in ["Step 1/2 : FROM python", "Successfully built abc123"]:
                on_line(line)
        return 0, "Step 1/2 : FROM python\nSuccessfully built abc123\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    monkeypatch.setattr(tools, "_run_ssh_stream", fake_run_ssh_stream)

    build_tool = tools.build_docker_image_tool(settings)
    result = await build_tool.ainvoke(
        {
            "code_path": "/data/test",
            "image_name": "ontology/ontology-graph",
            "image_tag": "v1",
        }
    )
    data = json.loads(result)
    assert data["success"] is True
    assert data["image"] == "ontology/ontology-graph:v1"
    assert "Successfully built" in data["log"]


async def test_build_docker_image_streams_log_lines(settings, monkeypatch):
    """build_docker_image 必须逐行回调 on_line（前端实时展示构建过程的基础）。"""

    async def fake_run_ssh(s, command, timeout=60):
        if "echo FOUND" in command:
            return 0, "FOUND:/data/test/Dockerfile\n", ""
        return 0, "", ""

    streamed: list[str] = []

    async def fake_run_ssh_stream(s, command, timeout=600, on_line=None):
        if on_line:
            for line in ["#1 [1/2] FROM python:3.12", "#2 [2/2] RUN pip install"]:
                on_line(line)
        return 0, "#1 [1/2] FROM python:3.12\n#2 [2/2] RUN pip install\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    monkeypatch.setattr(tools, "_run_ssh_stream", fake_run_ssh_stream)

    build_tool = tools.build_docker_image_tool(settings)
    # 模拟 api.py 设置构建日志队列后，工具内 get_build_log_queue 能拿到队列
    build_queue = asyncio.Queue()
    tools.set_build_log_queue(build_queue)
    try:
        result = await build_tool.ainvoke(
            {
                "code_path": "/data/test",
                "image_name": "ontology/ontology-graph",
                "image_tag": "v1",
            }
        )
        data = json.loads(result)
        assert data["success"] is True
        # 队列里的条目格式：(tag, tool_name, line)
        assert build_queue.qsize() == 2
        _, tool, line1 = build_queue.get_nowait()
        assert tool == "build_docker_image"
        assert line1 == "#1 [1/2] FROM python:3.12"
    finally:
        tools.set_build_log_queue(None)


async def test_build_docker_image_failed(settings, monkeypatch):
    """build_docker_image 失败路径：docker build 返回非零。"""

    async def fake_run_ssh(s, command, timeout=60):
        # Dockerfile 检查通过，build 本身失败
        if "echo FOUND" in command:
            return 0, "FOUND:/data/test/Dockerfile\n", ""
        return 0, "", ""

    async def fake_run_ssh_stream(s, command, timeout=600, on_line=None):
        return 1, "", "docker build error"

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    monkeypatch.setattr(tools, "_run_ssh_stream", fake_run_ssh_stream)

    build_tool = tools.build_docker_image_tool(settings)
    result = await build_tool.ainvoke(
        {
            "code_path": "/data/test",
            "image_name": "ontology/ontology-graph",
            "image_tag": "v1",
        }
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "command_failed"


async def test_stop_container_success(settings, monkeypatch):
    """stop_container 成功路径。"""

    async def fake_run_ssh(s, command, timeout=60):
        return 0, "ontology-graph\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    stop_tool = tools.build_stop_container_tool(settings)
    result = await stop_tool.ainvoke({"container_name": "ontology-graph"})
    data = json.loads(result)
    assert data["success"] is True
    assert data["container_name"] == "ontology-graph"


async def test_remove_container_wrong_name(settings):
    """remove_container 错误的 container_name 必须返回 validation_error。"""
    rm_tool = tools.build_remove_container_tool(settings)
    result = await rm_tool.ainvoke({"container_name": "wrong-container"})
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_remove_container_success(settings, monkeypatch):
    """remove_container 成功路径：docker rm -f 返回 0。"""
    seen_cmd: list[str] = []

    async def fake_run_ssh(s, command, timeout=60):
        seen_cmd.append(command)
        return 0, "ontology-graph\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    rm_tool = tools.build_remove_container_tool(settings)
    result = await rm_tool.ainvoke({"container_name": "ontology-graph"})
    data = json.loads(result)
    assert data["success"] is True
    assert data["container_name"] == "ontology-graph"
    assert any("docker rm -f" in c for c in seen_cmd)


async def test_remove_container_failed(settings, monkeypatch):
    """remove_container 失败路径：docker rm 返回非零。"""

    async def fake_run_ssh(s, command, timeout=60):
        return 1, "", "docker rm error"

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    rm_tool = tools.build_remove_container_tool(settings)
    result = await rm_tool.ainvoke({"container_name": "ontology-graph"})
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "command_failed"


async def test_start_container_success(settings, monkeypatch):
    """start_container 成功路径：前置检查（同名不存在）→ run。"""
    seen_cmd: list[str] = []

    async def fake_run_ssh(s, command, timeout=60):
        seen_cmd.append(command)
        # 前置检查 docker ps -a --filter name=... 返回空（同名容器不存在）
        if "ps -a" in command:
            return 0, "", ""
        # docker run
        return 0, "container-id-123\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    start_tool = tools.build_start_container_tool(settings)
    result = await start_tool.ainvoke(
        {
            "container_name": "ontology-graph",
            "image": "ontology/ontology-graph:v1",
        }
    )
    data = json.loads(result)
    assert data["success"] is True
    # 第 1 个命令是前置检查，第 2 个是 docker run
    assert "ps -a" in seen_cmd[0]
    assert "docker run" in seen_cmd[1]
    # 不再含 docker rm -f
    assert not any("rm -f" in c for c in seen_cmd)


async def test_start_container_already_exists(settings, monkeypatch):
    """start_container 前置检查：同名容器已存在 → container_already_exists。"""

    async def fake_run_ssh(s, command, timeout=60):
        # 前置检查返回容器名（已存在）
        if "ps -a" in command:
            return 0, "ontology-graph\n", ""
        return 0, "", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    start_tool = tools.build_start_container_tool(settings)
    result = await start_tool.ainvoke(
        {
            "container_name": "ontology-graph",
            "image": "ontology/ontology-graph:v1",
        }
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "container_already_exists"


async def test_start_container_run_failed(settings, monkeypatch):
    """start_container 失败路径：前置检查通过，docker run 返回非零。"""

    async def fake_run_ssh(s, command, timeout=60):
        if "ps -a" in command:
            return 0, "", ""
        return 1, "", "docker run error"

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    start_tool = tools.build_start_container_tool(settings)
    result = await start_tool.ainvoke(
        {
            "container_name": "ontology-graph",
            "image": "ontology/ontology-graph:v1",
        }
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "command_failed"


async def test_check_service_health_healthy(settings, monkeypatch):
    """check_service_health 健康路径：容器 running + HTTP 200。"""

    async def fake_run_ssh(s, command, timeout=60):
        if "inspect" in command:
            return 0, "running\n", ""
        if "curl" in command:
            return 0, "200\n", ""
        return 0, "", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    health_tool = tools.build_check_service_health_tool(settings)
    result = await health_tool.ainvoke(
        {
            "container_name": "ontology-graph",
            "health_url": "http://127.0.0.1:8080/healthz",
        }
    )
    data = json.loads(result)
    assert data["success"] is True
    assert data["status"] == "healthy"
    assert data["container_status"] == "running"
    assert data["http_status"] == "200"


async def test_check_service_health_unhealthy(settings, monkeypatch):
    """check_service_health 不健康路径：容器 running + HTTP 500。"""

    async def fake_run_ssh(s, command, timeout=60):
        if "inspect" in command:
            return 0, "running\n", ""
        if "curl" in command:
            return 0, "500\n", ""
        return 0, "", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    health_tool = tools.build_check_service_health_tool(settings)
    result = await health_tool.ainvoke(
        {
            "container_name": "ontology-graph",
        }
    )
    data = json.loads(result)
    assert data["success"] is True
    assert data["status"] == "unhealthy"
    assert data["http_status"] == "500"


async def test_build_tools_returns_15(settings):
    """build_tools 返回 15 个工具（8 个需审批写操作 + 7 个只读/巡检/文件操作）。"""
    tool_list = tools.build_tools(settings)
    assert len(tool_list) == 15
    names = [t.name for t in tool_list]
    assert "git_pull_code" in names
    assert "build_docker_image" in names
    assert "stop_container" in names
    assert "start_container" in names
    assert "check_service_health" in names
    assert "list_containers" in names
    assert "list_images" in names
    assert "check_dockerfile" in names


# ==================== 新增只读工具：list_containers / list_images / check_dockerfile ====================


async def test_list_containers_default(settings, monkeypatch):
    """list_containers 默认只看运行中容器（命令不含 -a）。"""
    seen_cmds: list[str] = []

    async def fake_run_ssh(s, command, timeout=60):
        seen_cmds.append(command)
        return (
            0,
            "ontology-graph|ontology/ontology-graph:v1|Up 2 hours|0.0.0.0:8080->8080/tcp\n",
            "",
        )

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    list_tool = tools.build_list_containers_tool(settings)
    result = await list_tool.ainvoke({})
    data = json.loads(result)
    assert data["success"] is True
    assert data["count"] == 1
    assert data["containers"][0]["name"] == "ontology-graph"
    assert data["containers"][0]["image"] == "ontology/ontology-graph:v1"
    # 默认不含 -a
    assert any("docker ps " in c and " -a " not in c for c in seen_cmds)


async def test_list_containers_include_all(settings, monkeypatch):
    """include_all=True 时命令含 -a（含已停止容器）。"""
    seen_cmds: list[str] = []

    async def fake_run_ssh(s, command, timeout=60):
        seen_cmds.append(command)
        return (
            0,
            "ontology-graph|ontology/ontology-graph:v1|Up 2 hours|\n"
            "old-app|old/image:v0|Exited (0) 3 days ago|\n",
            "",
        )

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    list_tool = tools.build_list_containers_tool(settings)
    result = await list_tool.ainvoke({"include_all": True})
    data = json.loads(result)
    assert data["success"] is True
    assert data["count"] == 2
    assert data["containers"][1]["status"].startswith("Exited")
    # 命令含 -a
    assert any(" -a " in c for c in seen_cmds)


async def test_list_containers_empty(settings, monkeypatch):
    """无容器时返回空列表 + count=0。"""

    async def fake_run_ssh(s, command, timeout=60):
        return 0, "", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    list_tool = tools.build_list_containers_tool(settings)
    result = await list_tool.ainvoke({})
    data = json.loads(result)
    assert data["success"] is True
    assert data["count"] == 0
    assert data["containers"] == []


async def test_list_containers_ssh_failed(settings, monkeypatch):
    """docker ps 失败路径：返回 command_failed。"""

    async def fake_run_ssh(s, command, timeout=60):
        return 1, "", "docker: command not found"

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    list_tool = tools.build_list_containers_tool(settings)
    result = await list_tool.ainvoke({})
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "command_failed"


async def test_list_images_success(settings, monkeypatch):
    """list_images 成功路径：解析 image|id|size 三段。"""

    async def fake_run_ssh(s, command, timeout=60):
        return (
            0,
            "ontology/ontology-graph:v1|a81f92c1234|523MB\n"
            "infra/data-service:v2|bb2233445566|310MB\n",
            "",
        )

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    images_tool = tools.build_list_images_tool(settings)
    result = await images_tool.ainvoke({})
    data = json.loads(result)
    assert data["success"] is True
    assert data["count"] == 2
    assert data["images"][0] == {
        "image": "ontology/ontology-graph:v1",
        "id": "a81f92c1234",
        "size": "523MB",
    }


async def test_list_images_empty(settings, monkeypatch):
    """无镜像时返回空列表 + count=0。"""

    async def fake_run_ssh(s, command, timeout=60):
        return 0, "", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    images_tool = tools.build_list_images_tool(settings)
    result = await images_tool.ainvoke({})
    data = json.loads(result)
    assert data["success"] is True
    assert data["count"] == 0


async def test_list_images_ssh_exception(settings, monkeypatch):
    """list_images SSH 异常路径。"""

    async def fake_run_ssh(s, command, timeout=60):
        raise ConnectionError("SSH connection refused")

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    images_tool = tools.build_list_images_tool(settings)
    result = await images_tool.ainvoke({})
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "ssh_error"


async def test_check_dockerfile_wrong_path(settings):
    """code_path 不在白名单内必须返回失败 JSON。"""
    check_tool = tools.build_check_dockerfile_tool(settings)
    result = await check_tool.ainvoke({"code_path": "/data/wrong-not-in-whitelist"})
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_check_dockerfile_root(settings, monkeypatch):
    """Dockerfile 在 code_path 根目录：返回 found_path。"""

    async def fake_run_ssh(s, command, timeout=60):
        assert "/data/test/Dockerfile" in command
        return 0, "FOUND:/data/test/Dockerfile\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    check_tool = tools.build_check_dockerfile_tool(settings)
    result = await check_tool.ainvoke({"code_path": "/data/test"})
    data = json.loads(result)
    assert data["success"] is True
    assert data["has_dockerfile"] is True
    assert data["found_path"] == "/data/test/Dockerfile"


async def test_check_dockerfile_subdir(settings, monkeypatch):
    """Dockerfile 在 docker/ 子目录：返回子目录路径。"""

    async def fake_run_ssh(s, command, timeout=60):
        assert "/data/test/docker/Dockerfile" in command
        return 0, "FOUND:/data/test/docker/Dockerfile\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    check_tool = tools.build_check_dockerfile_tool(settings)
    result = await check_tool.ainvoke({"code_path": "/data/test"})
    data = json.loads(result)
    assert data["success"] is True
    assert data["has_dockerfile"] is True
    assert data["found_path"] == "/data/test/docker/Dockerfile"


async def test_check_dockerfile_not_found(settings, monkeypatch):
    """未找到 Dockerfile：success=True + has_dockerfile=False + hint 引导。"""

    async def fake_run_ssh(s, command, timeout=60):
        return 0, "NOTFOUND\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    check_tool = tools.build_check_dockerfile_tool(settings)
    result = await check_tool.ainvoke({"code_path": "/data/test"})
    data = json.loads(result)
    assert data["success"] is True
    assert data["has_dockerfile"] is False
    assert data["found_path"] == ""
    assert "缺少 Dockerfile" in data["hint"]


# ==================== build_docker_image 联动：Dockerfile 检查 ====================


async def test_build_image_dockerfile_missing(settings, monkeypatch):
    """联动：code_path 缺 Dockerfile 时返回 dockerfile_missing，不执行 build。"""
    build_called = {"v": False}

    async def fake_run_ssh(s, command, timeout=60):
        if "FOUND" in command:
            return 0, "NOTFOUND\n", ""
        # 不应走到 docker build
        build_called["v"] = True
        return 0, "", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    build_tool = tools.build_docker_image_tool(settings)
    result = await build_tool.ainvoke(
        {
            "code_path": "/data/test",
            "image_name": "ontology/ontology-graph",
            "image_tag": "v1",
        }
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "dockerfile_missing"
    # 关键断言：不得执行 docker build
    assert build_called["v"] is False


async def test_build_image_with_subdir_dockerfile(settings, monkeypatch):
    """联动：Dockerfile 在 docker/ 子目录时 build 命令必须带 -f。"""
    seen_build_cmd: list[str] = []

    async def fake_run_ssh(s, command, timeout=60):
        if "FOUND" in command and "if [ -f" in command:
            return 0, "FOUND:/data/test/docker/Dockerfile\n", ""
        return 0, "", ""

    async def fake_run_ssh_stream(s, command, timeout=600, on_line=None):
        if "docker build" in command:
            seen_build_cmd.append(command)
        return 0, "Successfully built abc123\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    monkeypatch.setattr(tools, "_run_ssh_stream", fake_run_ssh_stream)

    build_tool = tools.build_docker_image_tool(settings)
    result = await build_tool.ainvoke(
        {
            "code_path": "/data/test",
            "image_name": "ontology/ontology-graph",
            "image_tag": "v1",
        }
    )
    data = json.loads(result)
    assert data["success"] is True
    # build 命令必须用 -f 指定子目录 Dockerfile
    assert len(seen_build_cmd) == 1
    assert "-f /data/test/docker/Dockerfile" in seen_build_cmd[0]
    # build context 仍是 code_path（项目根）；末尾 2>&1 是 stderr 合并重定向
    assert seen_build_cmd[0].strip().endswith("/data/test 2>&1")


# ==================== list_workspace_files ====================


async def test_list_workspace_files_wrong_workspace(settings):
    """workspace 不在白名单内必须返回 validation_error。"""
    tool = tools.build_list_workspace_files_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/etc", "subdir": ""}
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"
    assert data["workspace"] == "/etc"


async def test_list_workspace_files_subdir_traversal(settings):
    """subdir 含 .. 必须返回 validation_error（防越权）。"""
    tool = tools.build_list_workspace_files_tool(settings)
    # /data/deploy/workspace 在 _make_settings 的 workspaces 白名单内
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "subdir": "../../etc"}
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"
    assert ".." in data["message"]


async def test_list_workspace_files_success(settings, monkeypatch):
    """SSH 成功时返回结构化文件列表（不返回 raw 原始输出）。"""
    fake_ls_output = (
        "total 12\n"
        "drwxr-xr-x 2 root root 4.0K 2026-08-20 10:30 .\n"
        "drwxr-xr-x 3 root root 4.0K 2026-08-20 10:31 ..\n"
        "-rw-r--r-- 1 root root 1.2K 2026-08-20 10:30 Dockerfile\n"
        "drwxr-xr-x 2 root root 4.0K 2026-08-20 10:31 src\n"
    )

    async def fake_run_ssh(s, command, timeout=60):
        assert "ls -la" in command
        assert "--time-style=long-iso" in command
        return 0, fake_ls_output, ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    tool = tools.build_list_workspace_files_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "subdir": ""}
    )
    data = json.loads(result)
    assert data["success"] is True
    assert data["count"] == 2  # . 和 .. 被跳过，剩 Dockerfile + src
    # raw（原始 ls 输出）不再返回：冗余且会被 LLM 复述进对话文本
    assert "raw" not in data
    names = [f["name"] for f in data["files"]]
    assert "Dockerfile" in names
    assert "src" in names
    # 类型识别
    dockerfile = next(f for f in data["files"] if f["name"] == "Dockerfile")
    assert dockerfile["type"] == "file"
    src = next(f for f in data["files"] if f["name"] == "src")
    assert src["type"] == "dir"
    # 详细字段
    assert dockerfile["size"] == "1.2K"
    assert "2026-08-20" in dockerfile["modified"]


async def test_list_workspace_files_with_subdir(settings, monkeypatch):
    """subdir 非空时 target 拼接正确且命令含子目录。"""
    seen_cmd: list[str] = []

    async def fake_run_ssh(s, command, timeout=60):
        seen_cmd.append(command)
        return 0, "total 4\ndrwxr-xr-x 2 root root 4.0K 2026-08-20 10:30 .\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)

    tool = tools.build_list_workspace_files_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "subdir": "src"}
    )
    data = json.loads(result)
    assert data["success"] is True
    assert data["target"] == "/data/deploy/workspace/src"
    assert seen_cmd[0].endswith("/data/deploy/workspace/src")


# ==================== read_workspace_file ====================


async def test_read_workspace_file_wrong_workspace(settings):
    """workspace 不在白名单必须返回 validation_error。"""
    tool = tools.build_read_workspace_file_tool(settings)
    result = await tool.ainvoke({"workspace": "/etc", "file_path": "Dockerfile"})
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_read_workspace_file_traversal(settings):
    """file_path 含 .. 必须拒绝。"""
    tool = tools.build_read_workspace_file_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "file_path": "../../etc/passwd"}
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_read_workspace_file_absolute_path(settings):
    """file_path 绝对路径必须拒绝。"""
    tool = tools.build_read_workspace_file_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "file_path": "/etc/passwd"}
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_read_workspace_file_not_found(settings, monkeypatch):
    """文件不存在返回 not_found。"""
    async def fake_run_ssh(s, command, timeout=60):
        return 0, "__NOTEXISTS__\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_read_workspace_file_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "file_path": "missing.txt"}
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "not_found"


async def test_read_workspace_file_too_large(settings, monkeypatch):
    """文件超 1MB 返回 file_too_large。"""
    async def fake_run_ssh(s, command, timeout=60):
        return 0, "__TOOLARGE__2000000\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_read_workspace_file_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "file_path": "big.log"}
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "file_too_large"
    assert data["size"] == 2000000


async def test_read_workspace_file_success(settings, monkeypatch):
    """SSH 成功时返回文件内容。"""
    async def fake_run_ssh(s, command, timeout=60):
        assert "cat" in command
        return 0, "FROM python:3.12\nRUN pip install flask\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_read_workspace_file_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "file_path": "Dockerfile"}
    )
    data = json.loads(result)
    assert data["success"] is True
    assert "FROM python:3.12" in data["content"]
    assert data["target"] == "/data/deploy/workspace/Dockerfile"
    assert data["truncated"] is False


# ==================== write_workspace_file ====================


async def test_write_workspace_file_wrong_workspace(settings):
    """workspace 不在白名单必须返回 validation_error。"""
    tool = tools.build_write_workspace_file_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/etc", "file_path": "test.txt", "content": "hi"}
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_write_workspace_file_protect_git(settings):
    """写 .git/ 下文件必须拒绝。"""
    tool = tools.build_write_workspace_file_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "file_path": ".git/config", "content": "x"}
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"
    assert ".git" in data["message"]


async def test_write_workspace_file_too_large(settings):
    """content 超 256KB 必须拒绝。"""
    tool = tools.build_write_workspace_file_tool(settings)
    big_content = "x" * (256 * 1024 + 1)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "file_path": "big.txt", "content": big_content}
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "content_too_large"


async def test_write_workspace_file_success(settings, monkeypatch):
    """SSH 成功时命令含 base64 且返回 bytes_written。"""
    seen_cmd: list[str] = []

    async def fake_run_ssh(s, command, timeout=60):
        seen_cmd.append(command)
        return 0, "", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_write_workspace_file_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "file_path": "config.yaml", "content": "key: value\n"}
    )
    data = json.loads(result)
    assert data["success"] is True
    assert data["bytes_written"] == len("key: value\n".encode("utf-8"))
    # 命令含 base64 解码写入
    assert "base64 -d" in seen_cmd[0]
    assert "mkdir -p" in seen_cmd[0]


# ==================== delete_workspace_file ====================


async def test_delete_workspace_file_wrong_workspace(settings):
    """workspace 不在白名单必须返回 validation_error。"""
    tool = tools.build_delete_workspace_file_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/etc", "file_path": "test.txt"}
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_delete_workspace_file_protect_git(settings):
    """删 .git/ 下文件必须拒绝。"""
    tool = tools.build_delete_workspace_file_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "file_path": ".git/HEAD"}
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"
    assert ".git" in data["message"]


async def test_delete_workspace_file_is_directory(settings, monkeypatch):
    """目标是目录必须拒绝。"""
    async def fake_run_ssh(s, command, timeout=60):
        return 0, "__ISDIR__\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_delete_workspace_file_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "file_path": "src"}
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "is_directory"


async def test_delete_workspace_file_not_found(settings, monkeypatch):
    """文件不存在返回 not_found。"""
    async def fake_run_ssh(s, command, timeout=60):
        return 0, "__NOTEXISTS__\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_delete_workspace_file_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "file_path": "missing.txt"}
    )
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "not_found"


async def test_delete_workspace_file_success(settings, monkeypatch):
    """SSH 返回 __DELETED__ 时删除成功。"""
    seen_cmd: list[str] = []

    async def fake_run_ssh(s, command, timeout=60):
        seen_cmd.append(command)
        return 0, "__DELETED__\n", ""

    monkeypatch.setattr(tools, "_run_ssh", fake_run_ssh)
    tool = tools.build_delete_workspace_file_tool(settings)
    result = await tool.ainvoke(
        {"workspace": "/data/deploy/workspace", "file_path": "old_config.yaml"}
    )
    data = json.loads(result)
    assert data["success"] is True
    assert data["deleted"] == "/data/deploy/workspace/old_config.yaml"
    # 命令含 rm -f 且含 isdir 判断
    assert "rm -f" in seen_cmd[0]
    assert "-d" in seen_cmd[0]


# ==================== add_whitelist_entry / remove_whitelist_entry ====================


async def test_add_whitelist_entry_invalid_scope(tmp_path):
    """scope 非法必须返回 validation_error。"""
    s = _make_settings(whitelist_file=str(tmp_path / "w.json"))
    tool = tools.build_add_whitelist_entry_tool(s)
    result = await tool.ainvoke({"scope": "workspace", "value": "x"})
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "validation_error"


async def test_add_whitelist_entry_bad_value(tmp_path):
    """value 为空或含逗号必须返回 validation_error。"""
    s = _make_settings(whitelist_file=str(tmp_path / "w.json"))
    tool = tools.build_add_whitelist_entry_tool(s)
    result = await tool.ainvoke({"scope": "container", "value": ""})
    assert json.loads(result)["error_type"] == "validation_error"
    result = await tool.ainvoke({"scope": "container", "value": "a,b"})
    assert json.loads(result)["error_type"] == "validation_error"


async def test_add_whitelist_entry_already_exists(tmp_path):
    """条目已存在必须返回 already_exists。"""
    s = _make_settings(whitelist_file=str(tmp_path / "w.json"))
    tool = tools.build_add_whitelist_entry_tool(s)
    result = await tool.ainvoke({"scope": "container", "value": "ontology-graph"})
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "already_exists"


async def test_add_whitelist_entry_success(tmp_path):
    """添加成功：内存更新 + JSON 写入（两个 scope 都在 JSON 里）。"""
    wf = tmp_path / "w.json"
    s = _make_settings(whitelist_file=str(wf))
    tool = tools.build_add_whitelist_entry_tool(s)
    result = await tool.ainvoke({"scope": "container", "value": "my-app"})
    data = json.loads(result)
    assert data["success"] is True
    assert data["whitelist"] == ["ontology-graph", "my-app"]
    # 内存即时生效
    assert s.container_names == ["ontology-graph", "my-app"]
    # JSON 持久化：两个 scope 都写入
    saved = json.loads(wf.read_text(encoding="utf-8"))
    assert saved["container_names"] == ["ontology-graph", "my-app"]
    assert saved["image_prefixes"] == ["ontology/ontology-graph", "infra/data-service"]


async def test_add_whitelist_entry_image_scope(tmp_path):
    """image scope 添加镜像前缀成功。"""
    s = _make_settings(whitelist_file=str(tmp_path / "w.json"))
    tool = tools.build_add_whitelist_entry_tool(s)
    result = await tool.ainvoke({"scope": "image", "value": "infra/new-app"})
    data = json.loads(result)
    assert data["success"] is True
    assert "infra/new-app" in data["whitelist"]
    assert s.image_prefixes[-1] == "infra/new-app"


async def test_remove_whitelist_entry_not_found(tmp_path):
    """条目不存在必须返回 not_found。"""
    s = _make_settings(whitelist_file=str(tmp_path / "w.json"))
    tool = tools.build_remove_whitelist_entry_tool(s)
    result = await tool.ainvoke({"scope": "container", "value": "ghost"})
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "not_found"


async def test_remove_whitelist_entry_cannot_remove_last(tmp_path):
    """白名单只剩 1 条时禁止删除（防删空）。"""
    s = Settings(
        container_names_raw="only-one",
        workspaces_raw="/data/deploy/workspace",
        image_prefixes_raw="a/b",
        whitelist_file=str(tmp_path / "w.json"),
        server_host="10.1.248.143",
        server_port=22,
        server_user="root",
        server_password="secret",
        gitlab_user="user",
        gitlab_token="token",
        health_url="http://127.0.0.1:8080/healthz",
    )
    tool = tools.build_remove_whitelist_entry_tool(s)
    result = await tool.ainvoke({"scope": "container", "value": "only-one"})
    data = json.loads(result)
    assert data["success"] is False
    assert data["error_type"] == "cannot_remove_last"
    # 白名单未变
    assert s.container_names == ["only-one"]


async def test_remove_whitelist_entry_success(tmp_path):
    """删除成功：内存更新 + JSON 同步。"""
    wf = tmp_path / "w.json"
    s = _make_settings(whitelist_file=str(wf))
    # 先加一个，凑够 2 条才能删
    add_tool = tools.build_add_whitelist_entry_tool(s)
    await add_tool.ainvoke({"scope": "container", "value": "my-app"})

    tool = tools.build_remove_whitelist_entry_tool(s)
    result = await tool.ainvoke({"scope": "container", "value": "my-app"})
    data = json.loads(result)
    assert data["success"] is True
    assert data["whitelist"] == ["ontology-graph"]
    assert s.container_names == ["ontology-graph"]
    saved = json.loads(wf.read_text(encoding="utf-8"))
    assert saved["container_names"] == ["ontology-graph"]


async def test_settings_loads_whitelist_json_on_init(tmp_path):
    """model_post_init：whitelist.json 存在时覆盖 .env 初始值。"""
    wf = tmp_path / "w.json"
    wf.write_text(
        json.dumps({"container_names": ["from-json"], "image_prefixes": ["j/p"]}),
        encoding="utf-8",
    )
    s = Settings(
        container_names_raw="from-env",
        workspaces_raw="/data/deploy/workspace",
        image_prefixes_raw="e/p",
        whitelist_file=str(wf),
        server_host="10.1.248.143",
        server_port=22,
        server_user="root",
        server_password="secret",
        gitlab_user="user",
        gitlab_token="token",
        health_url="http://127.0.0.1:8080/healthz",
    )
    # JSON 优先于构造参数
    assert s.container_names == ["from-json"]
    assert s.image_prefixes == ["j/p"]
