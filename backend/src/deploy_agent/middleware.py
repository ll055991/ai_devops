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

from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from loguru import logger

from deploy_agent.settings import Settings


def _thread_id_from_runtime(runtime: Any) -> str | None:
    """从 runtime.context 提取 thread_id（简化版，参考项目 _thread_id_from_runtime）。

    runtime.context 通常是 config dict，形如 {"configurable": {"thread_id": "xxx"}}。
    """
    ctx = getattr(runtime, "context", None)
    if isinstance(ctx, dict):
        configurable = ctx.get("configurable", {})
        if isinstance(configurable, dict):
            tid = configurable.get("thread_id")
            if isinstance(tid, str):
                return tid
    return None


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
    """

    state_schema = AgentState

    def __init__(self, settings: Settings, *, tool_names: list[str] | None = None):
        self.settings = settings
        # tool_names 缺省取 settings 的审批名单
        self.tool_names = set(tool_names if tool_names is not None else settings.approval_tool_names())
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
        """模型输出后检查 tool_calls，对审批名单内的工具触发 interrupt。

        必须在 after_model 调用 interrupt()，不能在 wrap_tool_call。
        """
        messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
        if not messages:
            return None

        last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

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
            return None

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
                    logger.warning(
                        "APPROVAL | event=rejected | thread_id={} | tool={} | reason={}",
                        thread_id,
                        tool_call["name"],
                        _extract_tool_message_text(tool_message),
                    )
                    assistant_notes.append(_extract_tool_message_text(tool_message))
            else:
                revised_tool_calls.append(tool_call)

        last_ai_msg.tool_calls = revised_tool_calls
        for note in assistant_notes:
            _append_ai_message_text(last_ai_msg, note)
        return {"messages": [last_ai_msg]}

    async def aafter_model(self, state: AgentState[Any], runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)


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


__all__ = [
    "DeployApprovalMiddleware",
    "EnvScopingMiddleware",
]
