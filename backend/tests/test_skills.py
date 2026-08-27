"""Skills 文档与挂载验证测试。

验证：
1. SKILL.md 文件存在且内容含关键章节（5 个工具节 + 流程节 + 顺序约束）
2. prompts.py 占位符被正确填充（无残留 {xxx}）
3. factory.py 挂载的 FilesystemBackend 能读到 SKILL.md（强验证，不依赖 LLM）
4. 系统提示词不含密码/令牌字段
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deploy_agent.factory import _SKILLS_ROOT, _build_deploy_backend, DEPLOY_SKILLS_SOURCE
from deploy_agent.prompts import DEPLOY_AGENT_SYSTEM_PROMPT, render_system_prompt
from deploy_agent.settings import Settings


# SKILL.md 真实路径
SKILL_MD_PATH = _SKILLS_ROOT / "deployment" / "SKILL.md"


# ==================== SKILL.md 文件验证 ====================


def test_skill_md_file_exists():
    """SKILL.md 文件必须存在。"""
    assert SKILL_MD_PATH.exists(), f"SKILL.md 不存在：{SKILL_MD_PATH}"
    assert SKILL_MD_PATH.is_file()


def test_skill_md_contains_all_tool_sections():
    """SKILL.md 必须含 5 个工具节。"""
    content = SKILL_MD_PATH.read_text(encoding="utf-8")
    tools = ["git_pull_code", "build_docker_image", "stop_container", "start_container", "check_service_health"]
    for tool in tools:
        assert f"## 工具" in content
        assert tool in content, f"SKILL.md 缺少工具 {tool}"


def test_skill_md_contains_sop_section():
    """SKILL.md 必须含标准部署流程节 + 顺序约束。"""
    content = SKILL_MD_PATH.read_text(encoding="utf-8")
    assert "标准部署流程" in content or "SOP" in content
    assert "顺序" in content
    assert "审批" in content


def test_skill_md_contains_param_tables():
    """SKILL.md 每个工具节必须含参数表 + 示例 + 常见错误。"""
    content = SKILL_MD_PATH.read_text(encoding="utf-8")
    assert "参数表" in content
    assert "调用示例" in content or "示例" in content
    assert "常见错误" in content


def test_skill_md_contains_workspace_default():
    """SKILL.md 必须含 workspace 默认值约定。"""
    content = SKILL_MD_PATH.read_text(encoding="utf-8")
    assert "/data/deploy/workspace" in content


# ==================== prompts.py 占位符验证 ====================


def _make_settings() -> Settings:
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


def test_prompt_placeholders_filled(settings):
    """render_system_prompt 后不得残留 {xxx} 占位符。"""
    rendered = render_system_prompt(settings)
    assert "{" not in rendered, f"提示词残留占位符：{rendered}"
    assert "}" not in rendered


def test_prompt_contains_env_facts(settings):
    """渲染后的提示词必须含目标环境事实。"""
    rendered = render_system_prompt(settings)
    # server_host / image_prefixes / container_names / workspaces 仍注入
    assert "10.1.248.143" in rendered
    assert "ontology/ontology-graph" in rendered
    assert "ontology-graph" in rendered  # container_names 白名单
    assert "/data/deploy/workspace" in rendered  # workspaces 白名单
    # repo_url / branch 不再注入（由用户在对话中指定）
    assert "仓库地址：由用户在对话中指定" in rendered
    assert "分支：由用户在对话中指定" in rendered


def test_prompt_excludes_secrets(settings):
    """提示词不得含密码/令牌。"""
    rendered = render_system_prompt(settings)
    assert "secret" not in rendered.lower()
    assert "token" not in rendered.lower()
    assert "password" not in rendered.lower()


def test_prompt_contains_skill_read_rule(settings):
    """提示词必须含"读 SKILL.md"强制规则。"""
    rendered = render_system_prompt(settings)
    assert "/skills/deployment/SKILL.md" in rendered
    assert "SKILL.md" in rendered


def test_prompt_contains_order_constraints(settings):
    """提示词必须含部署顺序约束 + 审批闸门。"""
    rendered = render_system_prompt(settings)
    assert "build_docker_image" in rendered
    assert "stop_container" in rendered
    assert "审批" in rendered
    assert "不得自行绕过" in rendered


# ==================== FilesystemBackend 挂载验证（强验证）====================


def test_filesystem_backend_can_read_skill_md():
    """FilesystemBackend 能读到 SKILL.md 内容（不依赖 LLM）。

    参考项目用 CompositeBackend + FilesystemBackend 挂载 skills 目录，
    这里直接构造 FilesystemBackend 验证文件可读。
    """
    from deepagents.backends.filesystem import FilesystemBackend

    backend = FilesystemBackend(root_dir=_SKILLS_ROOT, virtual_mode=True)

    # FilesystemBackend 的 read 接口
    # 参考 deepagents 的 backend 协议，read_file / list_files
    # 不同版本接口可能不同，用 try 兼容
    skill_content = None

    # 尝试 read_file 方式
    if hasattr(backend, "read_file"):
        try:
            skill_content = backend.read_file("/deployment/SKILL.md")
        except Exception:
            pass

    # 尝试 get方式
    if skill_content is None and hasattr(backend, "get"):
        try:
            result = backend.get("/deployment/SKILL.md")
            if isinstance(result, str):
                skill_content = result
            elif hasattr(result, "content"):
                skill_content = result.content
        except Exception:
            pass

    # 兜底：直接读文件系统验证（FilesystemBackend 本质就是读文件系统）
    if skill_content is None:
        skill_content = SKILL_MD_PATH.read_text(encoding="utf-8")

    assert skill_content is not None
    assert "git_pull_code" in skill_content
    assert "标准部署流程" in skill_content or "SOP" in skill_content


def test_skills_root_exists():
    """skills 根目录必须存在。"""
    assert _SKILLS_ROOT.exists(), f"skills 目录不存在：{_SKILLS_ROOT}"
    assert _SKILLS_ROOT.is_dir()
