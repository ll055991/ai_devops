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
from deepagents.middleware.subagents import SubAgent
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from loguru import logger

from deploy_agent.middleware import (
    AuditLogMiddleware,
    DeployApprovalMiddleware,
    DeploymentStateMiddleware,
    EnvScopingMiddleware,
    RiskControlMiddleware,
)
from deploy_agent.prompts import render_system_prompt
from deploy_agent.runtime import RuntimeContext
from deploy_agent.settings import Settings, get_settings
from deploy_agent.tools import build_tools

# Agent 名称
DEPLOY_AGENT_NAME = "deploy-agent"

# 模型最大 token 数（与 .env MODEL_MAX_TOKENS=8192 对齐）


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
    - max_tokens 从 settings.model_max_tokens 读取（.env 的 MODEL_MAX_TOKENS）
    - api_key / base_url 从 settings 读取


    """
    logger.debug(
        "构建聊天模型 | model={} | base_url={} | max_tokens={}",
        settings.openai_model,
        settings.openai_base_url,
        settings.model_max_tokens,
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
        "max_tokens": settings.model_max_tokens,
    }

    if settings.openai_api_key is not None:
        model_kwargs["api_key"] = settings.openai_api_key.get_secret_value()
    if settings.openai_base_url is not None:
        model_kwargs["base_url"] = str(settings.openai_base_url)

    return ChatOpenAI(**model_kwargs)


def _build_deploy_backend():
    """构建部署 Agent 的 backend。

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


# 子 Agent 工具主责划分（ADR-0005 第一阶段：上下文隔离 + 高风险上收主 Agent）。
# 只读工具允许被多个子 Agent 共享；审批名单内的写操作工具一律不进子 Agent。
CODE_AGENT_TOOLS = ("git_pull_code", "check_dockerfile", "list_workspace_files", "read_workspace_file")
BUILD_AGENT_TOOLS = ("check_dockerfile", "build_docker_image", "list_images")
DEPLOY_PLAN_AGENT_TOOLS = ("list_containers", "get_deployment_status", "list_deployment_history")
MONITOR_AGENT_TOOLS = (
    "check_service_health",
    "get_container_logs",
    "list_containers",
    "get_deployment_status",
    "list_deployment_history",
    "check_server_environment",
)


def _build_subagent_middleware(settings: Settings) -> list[Any]:
    """子 Agent 工具层中间件：AuditLog（最外层）→ RiskControl → DeploymentState。

    子 Agent 是独立编译的嵌套图，主 Agent 的中间件不会自动生效，
    必须显式挂载，否则：工具调用不落审计、不更新部署状态、绕过风控前置校验。

    不带 DeployApproval：审批名单内的高风险写工具按 ADR-0005 不上收子 Agent，
    且嵌套子图没有 checkpointer/interrupt 恢复链路，interrupt() 无法工作。
    """
    return [
        AuditLogMiddleware(settings),
        RiskControlMiddleware(settings),
        DeploymentStateMiddleware(settings),
    ]


def build_subagents(all_tools: list, settings: Settings) -> list[SubAgent]:
    """按工具主责切分子 Agent（只读共享、高风险写独占主 Agent）。

    Args:
        all_tools: build_tools 返回的 20 个业务工具。
        settings: 用于给子 Agent 构建工具层中间件（审计/风控/状态）。

    Returns:
        4 个 SubAgent 规格：code / build / deploy-plan / monitor。
    """
    by_name = {t.name: t for t in all_tools}
    guard_middleware = _build_subagent_middleware(settings)

    def pick(names: tuple) -> list:
        return [by_name[n] for n in names if n in by_name]

    return [
        SubAgent(
            name="code-agent",
            description="代码拉取专家：git 拉码、Dockerfile 确认、工作区文件只读查看。主流程第一步调用。",
            system_prompt="你是代码拉取专家。只用分配给你的只读工具完成拉码与文件确认，输出 commit hash 与 Dockerfile 位置。需要写文件或改白名单时只输出计划，不编造工具调用。",
            tools=pick(CODE_AGENT_TOOLS),
            middleware=guard_middleware,
        ),
        SubAgent(
            name="build-agent",
            description="镜像构建专家：Dockerfile 检查、docker build、构建日志分析。code-agent 成功后调用。",
            system_prompt="你是镜像构建专家。只用分配给你的工具确认 Dockerfile、构建镜像并分析构建日志，输出镜像名与构建结论。构建失败时直接报告，不进入后续步骤。",
            tools=pick(BUILD_AGENT_TOOLS),
            middleware=guard_middleware,
        ),
        SubAgent(
            name="deploy-planner",
            description="部署规划专家：只读巡检容器与部署历史，输出停旧删旧起新计划。高风险启停删回滚由主 Agent 执行，不经手此 Agent。",
            system_prompt="你是部署规划专家。只能用只读工具巡检容器与部署历史，输出停旧、删旧、起新三步计划（含容器名与镜像名）。禁止执行高风险操作，只规划不执行。",
            tools=pick(DEPLOY_PLAN_AGENT_TOOLS),
            middleware=guard_middleware,
        ),
        SubAgent(
            name="monitor-agent",
            description="部署巡检专家：容器状态、运行时日志、健康检查三过判定，给出成功或失败结论。部署完成后调用。",
            system_prompt="你是部署巡检专家。只用只读工具采集容器状态、运行时日志与健康检查，按三过标准判定：容器 running、HTTP 200、日志无 ERROR 全过才算成功。只判不定夺，回滚由主 Agent 发起。",
            tools=pick(MONITOR_AGENT_TOOLS),
            middleware=guard_middleware,
        ),
    ]


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
    all_tools = build_tools(resolved_settings)

    logger.info("创建部署 Agent | name={}", DEPLOY_AGENT_NAME)

    return create_deep_agent(
        name=DEPLOY_AGENT_NAME,
        model=build_chat_model(resolved_settings),
        tools=all_tools,
        system_prompt=system_prompt,
        # 上下文 schema，对应 runtime.py 的 RuntimeContext
        context_schema=RuntimeContext,
        checkpointer=resolved_checkpointer,
        # 注册中间件（列表顺序：第一个在最外层）：
        # - AuditLogMiddleware 审计落库（最外层，能捕获 RiskControl 拒绝与工具异常）
        # - RiskControlMiddleware 并发锁 + 危险操作前置校验（拒绝时短路，不进入状态跟踪）
        # - EnvScopingMiddleware 参数透传（白名单校验已下沉到各 tool 内部）
        # - DeploymentStateMiddleware 工具执行后自动更新部署状态（commit/image/container/status）
        # - DeployApprovalMiddleware 在 after_model 触发审批中断
        middleware=[
            AuditLogMiddleware(resolved_settings),
            RiskControlMiddleware(resolved_settings),
            EnvScopingMiddleware(resolved_settings),
            DeploymentStateMiddleware(resolved_settings),
            DeployApprovalMiddleware(resolved_settings),
        ],
        # 挂载 skills 目录，Agent 通过 /skills/deployment/SKILL.md 读取技能文档
        skills=[DEPLOY_SKILLS_SOURCE],
        backend=_build_deploy_backend(),
        # 子 Agent：上下文隔离 + 高风险上收主 Agent（ADR-0005 第一阶段）。
        # 主 Agent 经 task() 委派；高风险写操作工具只在主 Agent 手里，
        # 复用主层 after_model 审批与审计风控状态全链路，子 Agent 无法绕过。
        subagents=build_subagents(all_tools, resolved_settings),
    )
