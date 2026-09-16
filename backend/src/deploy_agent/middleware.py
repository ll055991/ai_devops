"""部署 Agent 中间件。

对应需求文档第五章「审批机制」：
1. DeployApprovalMiddleware：参考 ConditionalAPIMiddleware，在 after_model 中
   对审批名单内的工具调用触发 interrupt()，支持 approve/reject 决策。
2. EnvScopingMiddleware：参考 OntologyIdScopingMiddleware，对白名单字段强制
   注入/覆盖，防止模型传参绕过白名单。

关键点（来自参考项目 ConditionalAPIMiddleware 注释）：
- interrupt() 必须在 after_model 调用，不能在 wrap_tool_call，否则 GraphInterrupt
  会被 ToolNode 的异常处理器静默吞掉。
- reject 决策返回错误 ToolMessage 并从 tool_calls 移除，避免下一轮 OpenAI
  因孤儿 ToolMessage 报协议错误。
- edit 决策在本 Demo 中不支持，按 reject 处理并提示。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from loguru import logger

from deploy_agent.settings import Settings


# 嵌套子图的 thread_id 通道：deepagents 调用子 Agent 时不透传 config
# （subagents.py 里 subagent.ainvoke(state) 无 config），子 Agent 内
# execution_info.thread_id 为 None。由最外层中间件在进入工具链前写入，
# 子 Agent 内的中间件读取，保证审计/状态记录归到同一会话。
_thread_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "deploy_thread_id", default=None
)


def _thread_id_from_runtime(runtime: Any) -> str | None:
    """从 runtime 提取 thread_id。

    顺序：runtime.execution_info.thread_id（langgraph 标准字段，由 config 填充）
    → runtime.context 字典（单元测试 _FakeRuntime 形状）→ _thread_id_ctx
    （子 Agent 嵌套图兜底，见上）。
    """
    info = getattr(runtime, "execution_info", None)
    tid = getattr(info, "thread_id", None)
    if isinstance(tid, str) and tid:
        return tid

    ctx = getattr(runtime, "context", None)
    if isinstance(ctx, dict):
        configurable = ctx.get("configurable", {})
        if isinstance(configurable, dict):
            tid = configurable.get("thread_id")
            if isinstance(tid, str):
                return tid

    return _thread_id_ctx.get()


def _unwrap_tool_result(result: Any) -> Any:
    """解包链内工具结果。

    ToolNode 交给中间件 handler 的是 ToolMessage，工具原生返回（str/JSON）
    在其 content 里；直接解析 ToolMessage 会导致状态误判（如健康检查恒为
    unhealthy、失败永远落不上库）。content 非 str/dict 时原样返回。
    """
    content = getattr(result, "content", None)
    if isinstance(content, (str, dict)):
        return content
    return result


def _extract_tool_message_text(message: ToolMessage) -> str:
    """提取 ToolMessage 的文本内容（参考项目同名函数）。"""
    content = message.content
    if isinstance(content, str):
        return content
    if hasattr(message, "text") and isinstance(message.text, str):
        return message.text
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(p for p in parts if p)
    return str(content)


def _append_ai_message_text(message: AIMessage, text: str) -> None:
    """向 AIMessage 追加文本内容（参考项目同名函数）。

    拒绝决策时把拒绝原因追加到 AI 消息，供下一轮模型理解。
    """
    if message.content:
        existing = message.content
        if isinstance(existing, str):
            message.content = f"{existing}\n\n{text}"
        elif isinstance(existing, list):
            message.content = [*existing, {"type": "text", "text": text}]
        else:
            message.content = f"{existing}\n\n{text}"
    else:
        message.content = text


def _mask_args(args: dict[str, Any]) -> str:
    """参数摘要用于日志（敏感字段打码）。"""
    safe: dict[str, Any] = {}
    for k, v in args.items():
        if k in {"password", "token", "api_key", "server_password", "gitlab_token"}:
            safe[k] = "***"
        elif isinstance(v, str) and len(v) > 40:
            safe[k] = f"{v[:6]}...{v[-4:]}"
        else:
            safe[k] = v
    return str(safe)


def _build_action_description(tool_call: dict[str, Any]) -> str:
    """生成审批中断的中文描述（形如"停止容器 ontology-graph"）。"""
    name = tool_call.get("name", "")
    args = tool_call.get("args", {})
    if not isinstance(args, dict):
        args = {}

    if name == "stop_container":
        return f"停止容器 {args.get('container_name', '<unknown>')}"
    if name == "start_container":
        return f"启动容器 {args.get('container_name', '<unknown>')} 使用镜像 {args.get('image', '<unknown>')}"
    if name == "build_docker_image":
        return f"构建镜像 {args.get('image_name', '<unknown>')}:{args.get('image_tag', '<unknown>')}"
    if name == "git_pull_code":
        return f"拉取代码 分支 {args.get('branch', '<unknown>')}"
    return f"执行工具 {name}"


class DeployApprovalMiddleware(AgentMiddleware):
    """部署审批中间件。

    对 settings.approval_tool_names() 名单内的工具调用触发人工审批：
    - after_model 中扫描 AIMessage.tool_calls
    - 命中则 interrupt({"action_requests": [...], "review_configs": [...]})
    - resume 值格式 {"decisions": [{"type": "approve"|"reject"|"edit", ...}]}
    - approve 保留 tool_call；reject 返回错误 ToolMessage 并从 tool_calls 移除
    - reject/edit 决策补记审计（status=rejected），见 _record_rejections
    """

    state_schema = AgentState

    def __init__(
        self,
        settings: Settings,
        *,
        tool_names: list[str] | None = None,
        audit_store: AuditLogStore | None = None,
    ):
        self.settings = settings
        # tool_names 缺省取 settings 的审批名单
        self.tool_names = set(tool_names if tool_names is not None else settings.approval_tool_names())
        # 审计库缺省用单例；测试注入临时库
        self.audit_store = audit_store if audit_store is not None else get_audit_store()
        super().__init__()

    def _build_action_request(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        """构造单个 action_request（参考 _build_action_request）。"""
        return {
            "name": tool_call.get("name", ""),
            "args": tool_call.get("args", {}),
            "description": _build_action_description(tool_call),
        }

    @staticmethod
    def _process_decision(
        decision: dict[str, Any],
        tool_call: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, ToolMessage | None]:
        """处理单条决策，返回 (revised_tool_call | None, tool_message | None)。

        - approve：保留原 tool_call
        - reject：返回错误 ToolMessage，tool_call 置 None
        - edit：不支持，按 reject 处理
        """
        decision_type = decision.get("type")

        if decision_type == "approve":
            return tool_call, None

        if decision_type == "reject":
            content = (
                decision.get("message")
                or f"用户拒绝了工具调用 `{tool_call['name']}`（id={tool_call['id']}）"
            )
            return None, ToolMessage(
                content=content,
                name=tool_call["name"],
                tool_call_id=tool_call["id"],
                status="error",
            )

        if decision_type == "edit":
            # Demo 简化：不支持编辑，按 reject 处理
            content = f"工具调用 `{tool_call['name']}` 的编辑决策暂不支持，已按拒绝处理。"
            return None, ToolMessage(
                content=content,
                name=tool_call["name"],
                tool_call_id=tool_call["id"],
                status="error",
            )

        raise ValueError(
            f"未知的决策类型: {decision}。"
            f"决策类型 '{decision_type}' 不在允许范围内。"
            f"期望 approve / reject / edit。"
        )

    def after_model(self, state: AgentState[Any], runtime: Any) -> dict[str, Any] | None:
        """同步路径：审批决策不落审计（图执行走 astream，实际用 aafter_model）。"""
        result, _ = self._run_approval(state, runtime)
        return result

    def _run_approval(
        self, state: AgentState[Any], runtime: Any
    ) -> tuple[dict[str, Any] | None, list[tuple[dict[str, Any], str]]]:
        """模型输出后检查 tool_calls，对审批名单内的工具触发 interrupt。

        必须在 after_model 调用 interrupt()，不能在 wrap_tool_call。

        Returns:
            (state 更新 | None, 被拒绝的 (tool_call, 原因) 列表)
        """
        messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
        if not messages:
            return None, []

        last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None, []

        thread_id = _thread_id_from_runtime(runtime) or "<unknown>"

        action_requests: list[dict[str, Any]] = []
        review_configs: list[dict[str, Any]] = []
        interrupt_indices: list[int] = []

        # 扫描 tool_calls，收集命中审批名单的
        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if tool_call.get("name") not in self.tool_names:
                continue
            action_requests.append(self._build_action_request(tool_call))
            review_configs.append({
                "action_name": tool_call["name"],
                "allowed_decisions": ["approve", "reject"],
            })
            interrupt_indices.append(idx)

        if not action_requests:
            return None, []

        # 记录每个被中断的工具
        for idx, req in zip(interrupt_indices, action_requests):
            logger.info(
                "APPROVAL | event=required | thread_id={} | tool={} | args={}",
                thread_id,
                req["name"],
                _mask_args(req["args"]),
            )

        hitl_request = {
            "action_requests": action_requests,
            "review_configs": review_configs,
        }

        # 触发中断，等待人工决策
        # interrupt() 在图执行时抛 GraphInterrupt；在单元测试中可 mock
        decisions: list[dict[str, Any]] = interrupt(hitl_request)["decisions"]

        # 记录决策
        for idx_pos, decision in enumerate(decisions):
            decision_type = decision.get("type") if isinstance(decision, dict) else "<invalid>"
            tool_name = action_requests[idx_pos]["name"] if idx_pos < len(action_requests) else "<unknown>"
            logger.info(
                "APPROVAL | event=decided | thread_id={} | decision_type={} | tool={}",
                thread_id,
                decision_type,
                tool_name,
            )

        # 单条决策广播到多个中断项（参考项目逻辑）
        if len(decisions) == 1 and len(interrupt_indices) > 1:
            only_decision = decisions[0]
            if isinstance(only_decision, dict) and only_decision.get("type") in {"approve", "reject"}:
                decisions = [only_decision] * len(interrupt_indices)

        # 决策数量必须匹配
        if (decisions_len := len(decisions)) != (interrupt_count := len(interrupt_indices)):
            logger.error(
                "APPROVAL | event=decision_mismatch | thread_id={} | decisions_len={} | interrupt_count={}",
                thread_id,
                decisions_len,
                interrupt_count,
                exc_info=True,
            )
            raise ValueError(
                f"决策数量 ({decisions_len}) 与待审批工具调用数量 ({interrupt_count}) 不匹配。"
            )

        revised_tool_calls: list[Any] = []
        assistant_notes: list[str] = []
        rejections: list[tuple[dict[str, Any], str]] = []
        decision_idx = 0

        # 应用决策：approve 保留，reject/edit 移除并追加错误 ToolMessage 文本
        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if idx in interrupt_indices:
                decision = decisions[decision_idx]
                decision_idx += 1
                revised_tool_call, tool_message = self._process_decision(decision, tool_call)
                if revised_tool_call is not None:
                    revised_tool_calls.append(revised_tool_call)
                if tool_message is not None:
                    # 拒绝时记录并追加错误文本到 AI 消息
                    reason = _extract_tool_message_text(tool_message)
                    logger.warning(
                        "APPROVAL | event=rejected | thread_id={} | tool={} | reason={}",
                        thread_id,
                        tool_call["name"],
                        reason,
                    )
                    assistant_notes.append(reason)
                    rejections.append((tool_call, reason))
            else:
                revised_tool_calls.append(tool_call)

        last_ai_msg.tool_calls = revised_tool_calls
        for note in assistant_notes:
            _append_ai_message_text(last_ai_msg, note)
        return {"messages": [last_ai_msg]}, rejections

    async def _record_rejections(
        self, runtime: Any, rejections: list[tuple[dict[str, Any], str]]
    ) -> None:
        """被拒绝的审批决策写审计（status=rejected）。写失败不阻断审批主流程。"""
        thread_id = _thread_id_from_runtime(runtime)
        for tool_call, reason in rejections:
            name = tool_call.get("name", "")
            args = tool_call.get("args")
            if not isinstance(args, dict):
                args = {}
            try:
                await self.audit_store.record(
                    name,
                    thread_id=thread_id,
                    risk_level=_TOOL_RISK_LEVELS.get(name, "low"),
                    args_summary=_mask_args(args),
                    status="rejected",
                    result_summary=reason[:200],
                )
            except Exception as exc:
                logger.warning(
                    "AUDIT | event=record_failed | tool={} | error={}",
                    name,
                    str(exc),
                )

    async def aafter_model(self, state: AgentState[Any], runtime: Any) -> dict[str, Any] | None:
        result, rejections = self._run_approval(state, runtime)
        if rejections:
            await self._record_rejections(runtime, rejections)
        return result


class EnvScopingMiddleware(AgentMiddleware):
    """环境作用域中间件（参数透传，校验移至 tool 内部）。

    历史职责：对白名单字段强制注入/覆盖，防止模型传参绕过白名单。
    当前职责：白名单从单值改为多值（CONTAINER_NAMES/WORKSPACES），
    强制覆盖单一值的语义不再适用，校验逻辑已下沉到各 tool 内部
    （git_pull_code 校验 workspace、stop/start_container 校验 container_name）。

    保留此类是为了不破坏 factory.py 的中间件注册链和测试用例，
    _inject_whitelist 仅做参数透传，不修改任何字段。
    """

    # 容器名相关工具（仅用于注释一致性，不再强制覆盖）
    _CONTAINER_NAME_TOOLS = {"stop_container", "start_container"}

    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__()

    def _inject_whitelist(self, tool_call: dict[str, Any]) -> None:
        """参数透传：白名单从单值改多值后，校验下沉到 tool 内部，此处不再覆盖。

        保留方法签名是为了不破坏 wrap_tool_call / awrap_tool_call 的调用链。
        """
        # 白名单校验逻辑已移至各 tool 内部（tools/__init__.py）：
        # - git_pull_code：workspace in settings.workspaces
        # - stop_container / start_container：container_name in settings.container_names
        # 此处刻意保留空方法体，避免改动 factory.py 的中间件注册顺序。
        return

    def wrap_tool_call(self, request: ToolCallRequest, handler):
        self._inject_whitelist(request.tool_call)
        return handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler):
        self._inject_whitelist(request.tool_call)
        return await handler(request)


# 延迟导入 interrupt，避免模块级导入时 langgraph 未就绪
# 参考项目在 factory.py 顶部 from langgraph.types import interrupt
# 这里放函数外但延迟到调用时，便于测试 monkeypatch
from langgraph.types import interrupt  # noqa: E402

from deploy_agent.state import DeploymentStateStore, get_deployment_store
from deploy_agent.audit import AuditLogStore, get_audit_store

# 工具风险分级表（RiskControl / AuditLog 共用）：
# - 只读巡检 = low；文件/白名单变更 = medium；容器启停删/回滚 = high
_TOOL_RISK_LEVELS: dict[str, str] = {
    "git_pull_code": "medium",
    "build_docker_image": "medium",
    "stop_container": "high",
    "remove_container": "high",
    "start_container": "high",
    "rollback_deployment": "high",
    "check_service_health": "low",
    "list_containers": "low",
    "list_images": "low",
    "check_dockerfile": "low",
    "check_server_environment": "low",
    "get_container_logs": "low",
    "list_workspace_files": "low",
    "read_workspace_file": "low",
    "write_workspace_file": "medium",
    "delete_workspace_file": "medium",
    "add_whitelist_entry": "medium",
    "remove_whitelist_entry": "medium",
    "get_deployment_status": "low",
    "list_deployment_history": "low",
}

# 风险控制拒绝的 error_type（审计状态记 blocked，不记 failed）
_BLOCKED_ERROR_TYPES = {
    "risk_blocked",
    "concurrency_conflict",
    "virtual_path_denied",
    "repeated_failure",
}


class DeploymentStateMiddleware(AgentMiddleware):
    """部署状态跟踪中间件（改造方案阶段 1）。

    在 wrap_tool_call 中包一层 handler：
    - 先执行工具拿到结果
    - 按工具类型 + 结果自动更新 deployments 表（commit/image/container/status）
    - 工具失败（success=false）时记录 status=failed + error
    - 只跟踪部署关键工具，list/文件/白名单工具不跟踪

    设计说明：
    - 工具全部为 async，实际执行路径是 awrap_tool_call；
      wrap_tool_call（同步）仅透传，与 EnvScopingMiddleware 保持一致。
    - thread_id 从 runtime.context 提取（复用 _thread_id_from_runtime），
      拿不到时不跟踪（不阻断工具执行）。
    - 工具结果非 JSON（无法解析）时跳过跟踪，记录 warning 日志。
    """

    # 需要跟踪部署状态的工具
    _TRACKED_TOOLS = {
        "git_pull_code",
        "build_docker_image",
        "stop_container",
        "remove_container",
        "start_container",
        "check_service_health",
        "rollback_deployment",
    }

    def __init__(self, settings: Settings, store: DeploymentStateStore | None = None):
        self.settings = settings
        # store 缺省用单例（backend/checkpoints/deployments.db）；测试注入临时库
        self.store = store if store is not None else get_deployment_store()
        super().__init__()

    @staticmethod
    def _status_for(tool_name: str, data: dict[str, Any]) -> str | None:
        """工具成功后的状态迁移（失败统一走 failed，由 _track 处理）。

        - build_docker_image 成功 → building
        - stop/remove/start_container 成功 → deploying
        - check_service_health：healthy/unhealthy 由工具 status 字段决定
        - rollback_deployment 成功 → rolled_back（回滚终态，rollback_from 指向被回滚记录）
        - git_pull_code 成功 → 不改状态（保持 pending）
        """
        if tool_name == "build_docker_image":
            return "building"
        if tool_name in ("stop_container", "remove_container", "start_container"):
            return "deploying"
        if tool_name == "check_service_health":
            return "healthy" if data.get("status") == "healthy" else "unhealthy"
        if tool_name == "rollback_deployment":
            return "rolled_back"
        return None

    @staticmethod
    def _fields_for(
        tool_name: str, args: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any]:
        """从工具参数与结果中提取要写入 deployments 的字段。"""
        if tool_name == "git_pull_code":
            # workspace 即部署环境标识
            return {
                "repo_url": args.get("repo_url"),
                "branch": args.get("branch"),
                "environment": args.get("workspace"),
                "commit": data.get("commit"),
            }
        if tool_name == "build_docker_image":
            return {"image": data.get("image")}
        if tool_name in ("stop_container", "remove_container", "start_container"):
            return {"container": args.get("container_name")}
        if tool_name == "check_service_health":
            return {"container": args.get("container_name")}
        if tool_name == "rollback_deployment":
            # 回滚：container/image 以工具结果为准（参数或历史记录解析而来），
            # rollback_from 记录被回滚的 deployment id（阶段 2 诊断闭环的关键关联）
            return {
                "container": data.get("container"),
                "image": data.get("image"),
                "rollback_from": data.get("rollback_from"),
            }
        return {}

    async def _track(self, request: ToolCallRequest, result: Any) -> None:
        """执行工具后更新部署状态。异常不阻断工具结果返回。"""
        tool_name = request.tool_call.get("name", "")
        if tool_name not in self._TRACKED_TOOLS:
            return

        thread_id = _thread_id_from_runtime(request.runtime)
        if not thread_id:
            logger.warning(
                "STATE | event=no_thread_id | tool={} | 跳过状态跟踪",
                tool_name,
            )
            return

        args = request.tool_call.get("args", {})
        if not isinstance(args, dict):
            args = {}

        # 链内 handler 返回的是 ToolMessage，先取 content（工具原生 str/JSON）
        # 再解析；否则健康检查恒为 unhealthy、失败永远落不上库
        result = _unwrap_tool_result(result)

        # 解析工具返回 JSON；解析失败则只记日志不跟踪（不阻断工具）
        data: dict[str, Any] = {}
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    data = parsed
            except (json.JSONDecodeError, ValueError):
                logger.warning(
                    "STATE | event=result_not_json | thread_id={} | tool={}",
                    thread_id,
                    tool_name,
                )
                return
        elif isinstance(result, dict):
            data = result

        try:
            if data.get("success") is False:
                # 工具失败：status=failed + error
                error_type = data.get("error_type") or "unknown"
                message = data.get("message") or ""
                await self.store.record(
                    thread_id,
                    status="failed",
                    error=f"{error_type}: {message}",
                    **self._fields_for(tool_name, args, data),
                )
                logger.info(
                    "STATE | event=deployment_failed | thread_id={} | tool={} | error={}",
                    thread_id,
                    tool_name,
                    error_type,
                )
                return

            # 工具成功：按类型迁移状态 + 写入关键字段
            fields = self._fields_for(tool_name, args, data)
            status = self._status_for(tool_name, data)
            if status:
                fields["status"] = status
            if fields:
                await self.store.record(thread_id, **fields)
                logger.info(
                    "STATE | event=deployment_updated | thread_id={} | tool={} | status={}",
                    thread_id,
                    tool_name,
                    status or "unchanged",
                )
        except Exception as exc:
            # 状态跟踪失败不影响工具执行结果
            logger.error(
                "STATE | event=track_failed | thread_id={} | tool={} | error={}",
                thread_id,
                tool_name,
                str(exc),
                exc_info=True,
            )

    def wrap_tool_call(self, request: ToolCallRequest, handler):
        """同步路径：仅透传（工具全为 async，实际走 awrap_tool_call）。"""
        return handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler):
        """异步路径：执行工具后更新部署状态。"""
        result = await handler(request)
        await self._track(request, result)
        return result


class RiskControlMiddleware(AgentMiddleware):
    """风险控制中间件（改造方案阶段 3）。

    职责：
    - 并发部署锁：按容器维度 asyncio.Lock，同一容器禁止两个会话并发部署
      （stop/remove/start/rollback），锁被占用时直接拒绝并返回结构化错误 JSON
    - 危险操作前置校验（基于部署状态库）：
      - remove_container：目标容器最近记录为 healthy（服务运行中）时拒绝，
        必须先 stop_container
      - rollback_deployment：目标容器最近记录为进行中（pending/building/deploying）
        时拒绝（回滚目标仍在部署中，防止状态错乱）
    - 高风险操作调用前打 warning 日志（审计明细由 AuditLogMiddleware 落库）
    - 文件工具边界守门：虚拟文件系统工具（read_file/ls/glob/grep）访问非挂载点
      路径时直接拒绝，并指引用 read_workspace_file（防小模型在"文件不存在"上死循环）

    拒绝时返回与工具结果同构的 JSON（success=false），不调用 handler，
    保证 LLM 能解析拒绝原因并如实汇报用户。
    """

    # 需要并发锁的容器部署写操作
    _LOCKED_TOOLS = {
        "stop_container",
        "remove_container",
        "start_container",
        "rollback_deployment",
    }

    # 虚拟文件系统工具（deepagents FilesystemMiddleware 提供）及其合法挂载点。
    # 这些工具读不到目标服务器文件，越界调用只会拿到 "not found" 文本，
    # 小模型会据此反复重试——在入口处拦掉并改指目标服务器工具。
    _VIRTUAL_FS_TOOLS = {"read_file", "ls", "glob", "grep"}
    _VIRTUAL_FS_ROOTS = ("/skills/", "/large_tool_results/")

    def __init__(
        self,
        settings: Settings,
        store: DeploymentStateStore | None = None,
    ):
        self.settings = settings
        # store 缺省用单例（部署状态库，前置校验数据源）；测试注入临时库
        self.store = store if store is not None else get_deployment_store()
        # 容器维度并发锁：key=容器名（或 rollback 的 deployment 键）
        self._locks: dict[str, asyncio.Lock] = {}
        # 越界文件路径被拒次数：key=thread:tool:path，用于升级指引（防反复重试）
        self._denied_paths: dict[str, int] = {}
        super().__init__()

    def _check_virtual_fs_path(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """虚拟文件系统路径守门：越界返回拒绝 JSON，合法返回 None。"""
        if tool_name not in self._VIRTUAL_FS_TOOLS:
            return None
        target = args.get("file_path") or args.get("path") or args.get("pattern")
        if not isinstance(target, str) or not target.startswith("/"):
            # 相对路径 / 无路径：走虚拟根目录，放行
            return None
        if target.startswith(self._VIRTUAL_FS_ROOTS) or target == "/":
            return None
        return {
            "success": False,
            "error_type": "virtual_path_denied",
            "message": (
                f"`{tool_name}` 只能访问虚拟文件系统挂载点（/skills/、/large_tool_results/），"
                f"读不到目标服务器路径 {target}。"
                "目标服务器的代码、Dockerfile、配置请改用 read_workspace_file / "
                "list_workspace_files / check_dockerfile（路径必须在 workspace 白名单内），"
                "容器日志用 get_container_logs。禁止再用相同参数调用本工具。"
            ),
            "path": target,
        }

    @staticmethod
    def _lock_key(tool_name: str, args: dict[str, Any]) -> str | None:
        """并发锁键：容器名；rollback 无容器名参数时退回 deployment_id 维度。"""
        if tool_name not in RiskControlMiddleware._LOCKED_TOOLS:
            return None
        if tool_name == "rollback_deployment":
            container = args.get("container_name")
            if container:
                return str(container)
            deployment_id = args.get("deployment_id")
            if deployment_id is not None:
                return f"deployment:{deployment_id}"
            return None
        container = args.get("container_name")
        return str(container) if container else None

    async def _precheck(
        self, tool_name: str, args: dict[str, Any]
    ) -> dict[str, Any] | None:
        """危险操作前置校验：返回拒绝 JSON（dict）或 None（放行）。"""
        # remove_container：目标容器最近记录为 healthy（服务运行中）→ 拒绝
        if tool_name == "remove_container":
            container = args.get("container_name")
            if container:
                try:
                    latest = await self.store.latest_by_container(str(container))
                except Exception as exc:
                    # 状态库异常不阻断调用（校验失败放行，审批兜底）
                    logger.warning(
                        "RISK | event=precheck_store_failed | tool={} | error={}",
                        tool_name,
                        str(exc),
                    )
                    return None
                if latest is not None and latest.get("status") == "healthy":
                    logger.warning(
                        "RISK | event=remove_healthy_blocked | container={}",
                        container,
                    )
                    return {
                        "success": False,
                        "error_type": "risk_blocked",
                        "message": (
                            f"容器 {container} 当前为 healthy（服务运行中），"
                            "禁止直接删除，必须先调用 stop_container"
                        ),
                        "container": container,
                    }

        # rollback_deployment：目标容器最近记录为进行中 → 拒绝
        if tool_name == "rollback_deployment":
            container = args.get("container_name")
            if container:
                try:
                    latest = await self.store.latest_by_container(str(container))
                except Exception as exc:
                    logger.warning(
                        "RISK | event=precheck_store_failed | tool={} | error={}",
                        tool_name,
                        str(exc),
                    )
                    return None
                if latest is not None and latest.get("status") in (
                    "pending",
                    "building",
                    "deploying",
                ):
                    logger.warning(
                        "RISK | event=rollback_in_progress_blocked | container={}",
                        container,
                    )
                    return {
                        "success": False,
                        "error_type": "risk_blocked",
                        "message": (
                            f"容器 {container} 正在部署中（{latest.get('status')}），"
                            "禁止回滚，请等待部署完成"
                        ),
                        "container": container,
                    }
        return None

    def wrap_tool_call(self, request: ToolCallRequest, handler):
        """同步路径：仅透传（工具全为 async，实际走 awrap_tool_call）。"""
        return handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler):
        """异步路径：并发锁 + 前置校验 + 高风险日志。"""
        tool_name = request.tool_call.get("name", "")
        args = request.tool_call.get("args", {})
        if not isinstance(args, dict):
            args = {}
        thread_id = _thread_id_from_runtime(request.runtime) or "<unknown>"

        # 高风险操作调用前日志（审计落库由 AuditLogMiddleware 负责）
        if _TOOL_RISK_LEVELS.get(tool_name) == "high":
            logger.warning(
                "RISK | event=high_risk_call | thread_id={} | tool={} | args={}",
                thread_id,
                tool_name,
                _mask_args(args),
            )

        # 文件工具边界守门：虚拟文件系统工具越界访问 → 拒绝并改指目标服务器工具
        path_block = self._check_virtual_fs_path(tool_name, args)
        if path_block is not None:
            deny_key = f"{thread_id}:{tool_name}:{path_block['path']}"
            denied = self._denied_paths.get(deny_key, 0) + 1
            self._denied_paths[deny_key] = denied
            if denied >= 2:
                # 已明确拒绝过仍重试：升级为硬停，切断死循环
                path_block = {
                    **path_block,
                    "error_type": "repeated_failure",
                    "message": (
                        f"同一越界调用已被拒绝 {denied} 次，禁止再次调用 `{tool_name}`。"
                        "请立即改用 read_workspace_file / list_workspace_files / "
                        "check_dockerfile，或向用户说明无法读取。"
                    ),
                }
            logger.warning(
                "RISK | event=virtual_path_denied | thread_id={} | tool={} | path={} | count={}",
                thread_id,
                tool_name,
                path_block["path"],
                denied,
            )
            return json.dumps(path_block, ensure_ascii=False)

        # 前置校验：拒绝时直接返回错误 JSON，不执行工具
        block = await self._precheck(tool_name, args)
        if block is not None:
            return json.dumps(block, ensure_ascii=False)

        # 并发部署锁：同键锁被占用 → 拒绝（不排队等待）
        lock_key = self._lock_key(tool_name, args)
        if lock_key is not None:
            lock = self._locks.setdefault(lock_key, asyncio.Lock())
            if lock.locked():
                logger.warning(
                    "RISK | event=concurrency_blocked | thread_id={} | tool={} | key={}",
                    thread_id,
                    tool_name,
                    lock_key,
                )
                return json.dumps(
                    {
                        "success": False,
                        "error_type": "concurrency_conflict",
                        "message": (
                            f"目标（{lock_key}）正在被其他会话部署操作占用，"
                            "请等待完成后再试"
                        ),
                        "lock_key": lock_key,
                    },
                    ensure_ascii=False,
                )
            await lock.acquire()
            try:
                return await handler(request)
            finally:
                lock.release()

        return await handler(request)


class AuditLogMiddleware(AgentMiddleware):
    """操作审计中间件（改造方案阶段 3）。

    职责：
    - awrap_tool_call 记录每次工具调用：thread_id、工具名、风险等级、
      打码参数摘要（复用 _mask_args）、耗时、结果状态
      （ok=成功 / failed=工具返回 success=false / blocked=风险控制拒绝 / error=执行异常）
    - 审批决策（reject/edit）由 DeployApprovalMiddleware 补记
      （status=rejected，见该类注释）
    - 注册在最外层：能捕获 RiskControl 的拒绝结果（blocked）与工具异常（error）

    记录失败不阻断工具执行（审计尽力而为）。
    """

    def __init__(self, settings: Settings, store: AuditLogStore | None = None):
        self.settings = settings
        # store 缺省用单例（backend/checkpoints/audit.db）；测试注入临时库
        self.store = store if store is not None else get_audit_store()
        super().__init__()

    @staticmethod
    def _summarize_result(result: Any) -> tuple[str, str | None]:
        """解析工具结果 → (status, result_summary)。

        - success=true → (ok, None)
        - 风险控制拒绝（risk_blocked / concurrency_conflict）→ (blocked, ...)
        - 其他 success=false → (failed, "error_type: message")
        - 非 JSON / 异常文本 → (failed, 截断文本)
        """
        # 链内结果先解包（ToolMessage → content），否则成功/失败全记 ok
        result = _unwrap_tool_result(result)
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except (json.JSONDecodeError, ValueError):
                text = result.strip().replace("\n", " ")
                return "failed", text[:200] or None
            if not isinstance(parsed, dict):
                return "ok", None
            if parsed.get("success") is False:
                error_type = parsed.get("error_type") or "unknown"
                message = parsed.get("message") or ""
                summary = f"{error_type}: {message}" if message else error_type
                status = "blocked" if error_type in _BLOCKED_ERROR_TYPES else "failed"
                return status, summary[:200]
            return "ok", None
        if isinstance(result, dict):
            if result.get("success") is False:
                error_type = str(result.get("error_type") or "unknown")
                status = "blocked" if error_type in _BLOCKED_ERROR_TYPES else "failed"
                return status, error_type[:200]
            return "ok", None
        return "ok", None

    def wrap_tool_call(self, request: ToolCallRequest, handler):
        """同步路径：仅透传（工具全为 async，实际走 awrap_tool_call）。"""
        return handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler):
        """异步路径：记录工具调用审计（打码参数 + 耗时 + 结果状态）。"""
        tool_name = request.tool_call.get("name", "")
        args = request.tool_call.get("args", {})
        if not isinstance(args, dict):
            args = {}
        thread_id = _thread_id_from_runtime(request.runtime)
        risk_level = _TOOL_RISK_LEVELS.get(tool_name, "low")
        args_summary = str(_mask_args(args))

        # 最外层中间件：把 thread_id 写入 ContextVar，供嵌套子 Agent 内的
        # 中间件兜底读取（子图不透传 config）。仅 set 不 reset：值随请求的
        # asyncio 任务上下文生命周期结束而消失，不跨请求泄漏。
        if thread_id is not None:
            _thread_id_ctx.set(thread_id)

        start = time.perf_counter()
        try:
            result = await handler(request)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            try:
                await self.store.record(
                    tool_name,
                    thread_id=thread_id,
                    risk_level=risk_level,
                    args_summary=args_summary,
                    status="error",
                    result_summary=str(exc)[:200],
                    elapsed_ms=elapsed_ms,
                )
            except Exception as store_exc:
                logger.warning(
                    "AUDIT | event=record_failed | tool={} | error={}",
                    tool_name,
                    str(store_exc),
                )
            raise
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        status, summary = self._summarize_result(result)
        try:
            await self.store.record(
                tool_name,
                thread_id=thread_id,
                risk_level=risk_level,
                args_summary=args_summary,
                status=status,
                result_summary=summary,
                elapsed_ms=elapsed_ms,
            )
        except Exception as store_exc:
            logger.warning(
                "AUDIT | event=record_failed | tool={} | error={}",
                tool_name,
                str(store_exc),
            )
        return result


__all__ = [
    "DeployApprovalMiddleware",
    "EnvScopingMiddleware",
    "DeploymentStateMiddleware",
    "RiskControlMiddleware",
    "AuditLogMiddleware",
]
