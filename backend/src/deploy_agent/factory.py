"""部署 Agent 工厂。

- build_chat_model：参考其 openai 分支，禁用 thinking，max_tokens=8192
- create_deploy_agent：参考 create_ai_native_agent 的 create_deep_agent 调用方式

"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
from deepagents import create_deep_agent
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.state import StateBackend
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from loguru import logger

from deploy_agent.middleware import DeployApprovalMiddleware, EnvScopingMiddleware
from deploy_agent.prompts import render_system_prompt
from deploy_agent.runtime import RuntimeContext
from deploy_agent.settings import Settings, get_settings
from deploy_agent.tools import build_tools

# Agent 名称
DEPLOY_AGENT_NAME = "deploy-agent"

# 模型最大 token 数（与 .env MODEL_MAX_TOKENS=8192 对齐）
# 参考 ontology_agent.agent.factory.build_chat_model 的 openai 分支，
# 该值作为 max_tokens 传给 ChatOpenAI
_MODEL_MAX_TOKENS = 8192

# Skills 虚拟挂载根路径（参考项目 AI_NATIVE_SKILLS_SOURCE = "/skills_ai_native/"）
# Agent 通过此路径读 /skills/deployment/SKILL.md
DEPLOY_SKILLS_SOURCE = "/skills/"

# skills 目录在文件系统中的真实路径（backend/skills/）
# 参考项目 _build_ontology_page_backend 的 skills_root 计算
_SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"


def build_chat_model(settings: Settings) -> BaseChatModel:
    """构建聊天模型。

    参考 ontology_agent.agent.factory.build_chat_model 的 openai 分支：
    - 禁用 thinking（extra_body.chat_template_kwargs.enable_thinking=False
      + thinking.type=disabled）
    - max_tokens=8192
    - api_key / base_url 从 settings 读取


    """
    logger.debug(
        "构建聊天模型 | model={} | base_url={} | max_tokens={}",
        settings.openai_model,
        settings.openai_base_url,
        _MODEL_MAX_TOKENS,
    )

    model_kwargs: dict[str, Any] = {
        "model": settings.openai_model,
        "temperature": settings.openai_temperature,
        # 禁用思考链：部分兼容 OpenAI 的模型（如 Qwen）支持 thinking 参数，
        # 显式关闭避免输出冗余思考内容
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},
            "thinking": {"type": "disabled"},
        },
        "max_tokens": _MODEL_MAX_TOKENS,
    }

    if settings.openai_api_key is not None:
        model_kwargs["api_key"] = settings.openai_api_key.get_secret_value()
    if settings.openai_base_url is not None:
        model_kwargs["base_url"] = str(settings.openai_base_url)

    return ChatOpenAI(**model_kwargs)


def _build_deploy_backend():
    """构建部署 Agent 的 backend（参考 _build_ontology_page_backend）。

    用 CompositeBackend 挂载 FilesystemBackend，让 Agent 能读 skills/ 目录下的 SKILL.md。
    virtual_mode=True 表示只读虚拟挂载，不会写文件。
    """
    def backend(runtime: Any) -> CompositeBackend:
        return CompositeBackend(
            default=StateBackend(runtime),
            routes={
                DEPLOY_SKILLS_SOURCE: FilesystemBackend(root_dir=_SKILLS_ROOT, virtual_mode=True),
            },
        )
    return backend


# 检查点数据库目录（backend/checkpoints/，与日志目录同级的项目根下）
_CHECKPOINT_DIR = Path(__file__).resolve().parents[2] / "checkpoints"


def build_default_checkpointer() -> AsyncSqliteSaver:
    """默认检查点：SQLite 持久化（backend/checkpoints/checkpoints.db）。

    对比 InMemorySaver：线程记忆落盘，后端进程重启后旧会话仍可续聊，
    /api/agent/threads 等记忆查询接口读取的也是这份数据。
    数据库文件由 aiosqlite 首次使用时自动创建，目录缺失时自动创建。
    """
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    db_path = _CHECKPOINT_DIR / "checkpoints.db"
    logger.info("检查点存储 | path={}", db_path)
    return AsyncSqliteSaver(aiosqlite.connect(str(db_path)))


def create_deploy_agent(
    settings: Settings | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """创建部署 Agent。

    - settings 缺省时用 get_settings() 单例
    - checkpointer 缺省时用 SQLite 持久化版本（build_default_checkpointer）
    - 调用 create_deep_agent 组装 Agent
    - 注册 DeployApprovalMiddleware（审批 stop/start container）
      + EnvScopingMiddleware（参数透传，白名单校验已下沉到各 tool 内部）
    - 挂载 skills/ 目录，Agent 可读 /skills/deployment/SKILL.md
    - 系统提示词用 settings 注入目标环境事实（密码不进提示词）
    - 仓库地址/分支由用户在对话中指定，不在此注入

    Args:
        settings: 配置对象，缺省从 .env 加载
        checkpointer: 检查点存储，缺省用 SQLite 持久化版本

    Returns:
        deepagents 编译后的 Agent
    """
    resolved_settings = settings or get_settings()
    resolved_checkpointer = (
        checkpointer if checkpointer is not None else build_default_checkpointer()
    )

    # 用 settings 注入目标环境事实（密码不进提示词）
    system_prompt = render_system_prompt(resolved_settings)

    logger.info("创建部署 Agent | name={}", DEPLOY_AGENT_NAME)

    return create_deep_agent(
        name=DEPLOY_AGENT_NAME,
        model=build_chat_model(resolved_settings),
        tools=build_tools(resolved_settings),
        system_prompt=system_prompt,
        # 上下文 schema，对应 runtime.py 的 RuntimeContext
        context_schema=RuntimeContext,
        checkpointer=resolved_checkpointer,
        # 注册中间件：
        # - EnvScopingMiddleware 参数透传（白名单校验已下沉到各 tool 内部）
        # - DeployApprovalMiddleware 在 after_model 触发审批中断
        middleware=[
            EnvScopingMiddleware(resolved_settings),
            DeployApprovalMiddleware(resolved_settings),
        ],
        # 挂载 skills 目录，Agent 通过 /skills/deployment/SKILL.md 读取技能文档
        skills=[DEPLOY_SKILLS_SOURCE],
        backend=_build_deploy_backend(),
    )
