"""部署 Agent 的 FastAPI 接口与 SSE 事件流。

对应需求文档第六、七章：
- POST /api/agent/chat：提交用户任务，返回 SSE 事件流
- GET /healthz：健康检查

SSE 事件类型（参考需求文档第九章 + 项目实际）：
- agent_state：Agent 运行状态（running/awaiting_approval/completed）
- tool_call_start：工具调用开始（name, args, call_id）
- tool_call_end：工具调用结束（name, result, call_id, log）
- message_delta：LLM 文本增量（text）
- log：工具返回的 log 字段切片（每 80 字符一条）
- approval_required：需要人工审批（action_requests, review_configs）
- task_status：任务状态推断（GIT_PULL/BUILD_IMAGE/...）
- stream_complete：流结束（final_result）
- error：异常（message）

流式翻译方式参考 ontology_agent.api.app.ai_native_invoke_stream：
agent.astream(input, config, stream_mode=["messages","updates"])
流结束后调 agent.aget_state(config).interrupts 判断是否被审批中断。
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from loguru import logger
from pydantic import BaseModel

from deploy_agent.factory import create_deploy_agent
from deploy_agent.settings import Settings, get_settings
from deploy_agent.tools import set_build_log_queue


# ==================== 工具名 → 任务状态映射 ====================
# 对应需求文档第八章任务状态设计
TOOL_STATUS_MAP: dict[str, str] = {
    "git_pull_code": "GIT_PULL",
    "build_docker_image": "BUILD_IMAGE",
    "stop_container": "STOP_CONTAINER",
    "remove_container": "REMOVE_CONTAINER",
    "start_container": "START_CONTAINER",
    "check_service_health": "HEALTH_CHECK",
    "list_containers": "LIST_CONTAINERS",
    "list_images": "LIST_IMAGES",
    "check_dockerfile": "CHECK_DOCKERFILE",
}

# log 字段切片长度（每条 log 事件最大字符数）
_LOG_CHUNK_SIZE = 80

# 心跳间隔：N 秒无数据则发 SSE 注释行保活，防客户端超时
_HEARTBEAT_INTERVAL = 15

# 工具名 → 友好提示（tool_call_start 事件附带 hint 字段）
TOOL_HINT_MAP: dict[str, str] = {
    "git_pull_code": "正在拉取代码...",
    "build_docker_image": "正在构建镜像，预计 2-5 分钟...",
    "stop_container": "正在停止旧容器...",
    "remove_container": "正在删除旧容器...",
    "start_container": "正在启动新容器...",
    "check_service_health": "正在检查服务健康状态...",
    "list_containers": "正在查询容器列表...",
    "list_images": "正在查询镜像列表...",
    "check_dockerfile": "正在检查 Dockerfile...",
    "list_workspace_files": "正在列出工作目录文件...",
    "read_workspace_file": "正在读取文件...",
    "write_workspace_file": "正在写入文件（需审批）...",
    "delete_workspace_file": "正在删除文件（需审批）...",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动无操作；退出时关闭检查点连接。

    避免 aiosqlite 后台线程在事件循环关闭后报 "Event loop is closed"。
    """
    yield
    saver = _checkpointer_instance
    if saver is not None:
        try:
            conn = getattr(saver, "conn", None)
            if conn is not None:
                await conn.close()
                logger.info("检查点存储连接已关闭")
        except Exception as exc:
            logger.warning("MEMORY | event=checkpointer_close_failed | error={}", str(exc))


app = FastAPI(title="deploy-agent", version="0.1.0", lifespan=lifespan)


# ==================== 请求 / 响应模型 ====================
class ChatRequest(BaseModel):
    """聊天请求体。"""

    message: str | None = None
    thread_id: str | None = None
    # 人工审批决策（approve/reject），恢复被中断的流时携带
    decisions: list[dict[str, Any]] | None = None


class HealthResponse(BaseModel):
    status: str


# ==================== SSE 辅助函数 ====================
def _sse_event(event: str, data: dict[str, Any]) -> str:
    """构造一条 SSE 事件（event 行 + data 行 JSON）。

    参考项目同名函数：f"event: {name}\\ndata: {json}\\n\\n"
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _safe_get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """安全嵌套取值。"""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _extract_text_from_message(message: Any) -> str:
    """从 LangChain Message 提取文本内容。"""
    if isinstance(message, str):
        return message
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _extract_tool_calls(message: Any) -> list[dict[str, Any]]:
    """从 AIMessage 提取 tool_calls。"""
    if not isinstance(message, AIMessage):
        return []
    return list(message.tool_calls or [])


def _split_log_to_chunks(log_text: str, chunk_size: int = _LOG_CHUNK_SIZE) -> list[str]:
    """把长 log 文本切成 chunk_size 字符的片段（防止单条 SSE 过大）。"""
    if not log_text:
        return []
    # 按行切后再按长度切，保留换行可读性
    lines = log_text.splitlines() or [log_text]
    chunks: list[str] = []
    for line in lines:
        if not line:
            continue
        # 单行超过 chunk_size 按长度切
        for i in range(0, len(line), chunk_size):
            chunks.append(line[i : i + chunk_size])
    return chunks


# 实时构建日志队列中的条目标记（与 astream chunk 区分开）
_BUILD_LOG_TAG = "__build_log__"


async def _await_queue_items(
    q1: asyncio.Queue, q2: asyncio.Queue
) -> list[tuple[asyncio.Queue, Any]] | None:
    """并发等待两个队列的已就绪元素，返回 [(来源队列, 元素), ...]。

    - 两个队列同时有货时全部返回（避免 FIRST_COMPLETED 丢弃另一队列元素）
    - 超时返回 None（调用方发心跳保活）
    - 未完成的 get 任务在 finally 中取消，元素保留在队列里
    """
    t1 = asyncio.create_task(q1.get())
    t2 = asyncio.create_task(q2.get())
    try:
        done, _ = await asyncio.wait(
            {t1, t2},
            timeout=_HEARTBEAT_INTERVAL,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            return None
        items: list[tuple[asyncio.Queue, Any]] = []
        for task, queue in ((t1, q1), (t2, q2)):
            if task.done() and not task.cancelled():
                try:
                    items.append((queue, task.result()))
                except Exception:
                    pass
        return items
    finally:
        for task in (t1, t2):
            if not task.done():
                task.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)


def _build_action_request_from_interrupt(interrupt_obj: Any) -> dict[str, Any]:
    """从 interrupt 对象提取 action_requests 信息。

    interrupt_obj.value 通常是 {"action_requests": [...], "review_configs": [...]}
    """
    value = getattr(interrupt_obj, "value", None)
    if isinstance(value, dict):
        return {
            "action_requests": value.get("action_requests", []),
            "review_configs": value.get("review_configs", []),
        }
    # value 可能直接是 list 或其他
    return {"action_requests": [], "review_configs": [], "raw": str(value)}


# ==================== FastAPI 请求中间件 ====================
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """记录每个 HTTP 请求的方法/路径/状态码/耗时/thread_id。"""
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    # 尝试从 query 或 path 提取 thread_id（POST 体的 thread_id 在日志中间件拿不到）
    thread_id = request.query_params.get("thread_id") or "<none>"

    logger.info(
        "HTTP | method={} | path={} | status={} | elapsed={}ms | thread_id={}",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        thread_id,
    )
    return response


# ==================== 健康检查 ====================
@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


# ==================== 记忆查询接口（打通前端历史会话） ====================
# 前端 localStorage 只存会话壳（标题/threadId），消息正文以这里为权威源：
# - GET /api/agent/threads：列出后端内存中全部线程（InMemorySaver，进程重启后清空）
# - GET /api/agent/threads/{thread_id}/messages：单个线程的完整消息历史
# 前端据此在启动时恢复/合并历史会话，多端/换浏览器也能找回对话。

# 消息文本截断长度（会话标题用）
_THREAD_TITLE_MAX = 20


def _extract_thread_messages(state_values: dict[str, Any]) -> list[dict[str, str]]:
    """把 checkpoint 的消息通道序列化为前端可用的 {role, content} 列表。

    只保留 HumanMessage(user) 与 AIMessage(assistant) 的文本，
    跳过 ToolMessage（工具结果在工具卡片/日志区展示，不进入消息气泡）。
    """
    msgs = state_values.get("messages") or state_values.get("messages+") or []
    out: list[dict[str, str]] = []
    for m in msgs:
        if isinstance(m, HumanMessage):
            out.append({"role": "user", "content": _extract_text_from_message(m)})
        elif isinstance(m, AIMessage):
            out.append({"role": "assistant", "content": _extract_text_from_message(m)})
    return out


def _title_from_thread_messages(msgs: list[Any]) -> str:
    """会话标题 = 第一条非空 user 消息，超长截断。"""
    for m in msgs:
        if isinstance(m, HumanMessage):
            text = _extract_text_from_message(m)
            if text:
                return text[:_THREAD_TITLE_MAX] + ("…" if len(text) > _THREAD_TITLE_MAX else "")
    return "新对话"


@app.get("/api/agent/threads")
async def list_threads() -> Any:
    """列出后端内存中全部会话线程（按更新时间倒序）。

    线程未创建（无 checkpointer）时返回空列表，前端按无历史处理。
    """
    try:
        checkpointer = _get_checkpointer()
        if checkpointer is None:
            return {"success": True, "threads": []}

        # alist 返回的是"每步一个 checkpoint 快照"，同一线程会出现多条；
        # 按 thread_id 分组，只保留 ts（时间戳）最新的一条
        latest_by_thread: dict[str, Any] = {}
        async for tuple_ in checkpointer.alist(None):
            config = tuple_.config or {}
            thread_id = (config.get("configurable") or {}).get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                continue
            prev = latest_by_thread.get(thread_id)
            if prev is not None:
                prev_cp = getattr(prev, "checkpoint", None) or {}
                cur_cp = tuple_.checkpoint or {}
                prev_ts = str(prev_cp.get("ts") or "") if isinstance(prev_cp, dict) else ""
                cur_ts = str(cur_cp.get("ts") or "") if isinstance(cur_cp, dict) else ""
                if cur_ts <= prev_ts:
                    continue
            latest_by_thread[thread_id] = tuple_

        threads: list[dict[str, Any]] = []
        for thread_id, tuple_ in latest_by_thread.items():
            checkpoint = getattr(tuple_, "checkpoint", None) or {}
            updated_at = checkpoint.get("ts") if isinstance(checkpoint, dict) else ""
            channel_values = checkpoint.get("channel_values", {}) if isinstance(checkpoint, dict) else {}
            msgs = channel_values.get("messages") or channel_values.get("messages+") or []
            threads.append(
                {
                    "thread_id": thread_id,
                    "updated_at": str(updated_at),
                    "message_count": len(_extract_thread_messages(channel_values)),
                    "title": _title_from_thread_messages(msgs),
                }
            )

        threads.sort(key=lambda t: t["updated_at"], reverse=True)
        logger.info("MEMORY | event=threads_listed | count={}", len(threads))
        return {"success": True, "threads": threads}
    except Exception as exc:
        logger.error("MEMORY | event=threads_list_failed | error={}", str(exc), exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})


@app.get("/api/agent/threads/{thread_id}/messages")
async def get_thread_messages(thread_id: str) -> Any:
    """返回单个线程的完整消息历史（前端恢复会话上下文用）。

    线程不存在返回 404 结构化错误；其余异常返回 500 结构化错误。
    """
    try:
        checkpointer = _get_checkpointer()
        config = {"configurable": {"thread_id": thread_id}}
        tuple_ = await checkpointer.aget_tuple(config) if checkpointer is not None else None
        if tuple_ is None:
            logger.info("MEMORY | event=thread_not_found | thread_id={}", thread_id)
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": f"线程不存在: {thread_id}"},
            )

        state = await get_agent().aget_state(config)
        values = state.values if isinstance(state.values, dict) else {}
        messages = _extract_thread_messages(values)
        logger.info(
            "MEMORY | event=thread_loaded | thread_id={} | messages={}",
            thread_id,
            len(messages),
        )
        return {"success": True, "thread_id": thread_id, "messages": messages}
    except Exception as exc:
        logger.error(
            "MEMORY | event=thread_load_failed | thread_id={} | error={}",
            thread_id,
            str(exc),
            exc_info=True,
        )
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})


@app.delete("/api/agent/threads/{thread_id}")
async def delete_thread(thread_id: str) -> Any:
    """删除指定线程的全部检查点数据（SQLite 持久化层物理删除）。

    前端删除对话时调用此接口，确保重启后端后已删会话不再出现。
    线程不存在时返回 200（幂等删除，视为成功）。
    """
    try:
        checkpointer = _get_checkpointer()
        if checkpointer is None:
            logger.info("MEMORY | event=delete_no_checkpointer | thread_id={}", thread_id)
            return {"success": True, "thread_id": thread_id, "deleted": 0}

        # 先检查线程是否存在，不存在时幂等返回成功（前端删除已删会话不报错）
        config = {"configurable": {"thread_id": thread_id}}
        tuple_ = await checkpointer.aget_tuple(config)
        if tuple_ is None:
            logger.info("MEMORY | event=thread_already_deleted | thread_id={}", thread_id)
            return {"success": True, "thread_id": thread_id, "deleted": 0}

        # 调用 AsyncSqliteSaver.adelete_thread 物理删除 checkpoints + writes
        await checkpointer.adelete_thread(thread_id)
        logger.info("MEMORY | event=thread_deleted | thread_id={}", thread_id)
        return {"success": True, "thread_id": thread_id, "deleted": 1}
    except Exception as exc:
        logger.error(
            "MEMORY | event=thread_delete_failed | thread_id={} | error={}",
            thread_id,
            str(exc),
            exc_info=True,
        )
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})


# ==================== Agent 单例（延迟初始化） ====================
_agent_instance: Any | None = None
_settings_instance: Settings | None = None
_checkpointer_instance: Any | None = None


def get_agent() -> Any:
    """获取 Agent 单例（首次调用时创建）。

    单例化避免每个请求重建 Agent（含 LLM 客户端、工具、检查点）。
    """
    global _agent_instance, _settings_instance, _checkpointer_instance
    if _agent_instance is None:
        _settings_instance = get_settings()
        _agent_instance = create_deploy_agent(_settings_instance)
        # 保存 checkpointer 引用：记忆查询接口读取 + 进程退出时优雅关闭连接
        _checkpointer_instance = getattr(_agent_instance, "checkpointer", None)
        logger.info("Agent 单例已创建")
    return _agent_instance


def _get_checkpointer() -> Any | None:
    """取 Agent 的 checkpointer（记忆存储）。Agent 未创建时返回 None。"""
    if _checkpointer_instance is not None:
        return _checkpointer_instance
    try:
        get_agent()
    except Exception as exc:
        logger.warning("MEMORY | event=agent_not_ready | error={}", str(exc))
        return None
    return _checkpointer_instance


# ==================== 主接口：POST /api/agent/chat ====================
@app.post("/api/agent/chat")
async def agent_chat(req: ChatRequest) -> StreamingResponse:
    """提交用户任务，返回 SSE 事件流。

    - 无 decisions：作为新对话开始
    - 有 decisions：恢复被审批中断的对话
    """
    agent = get_agent()
    settings = _settings_instance or get_settings()

    # thread_id 缺省生成（用时间戳，便于追踪）
    thread_id = req.thread_id or f"t-{int(time.time())}"
    config = {"configurable": {"thread_id": thread_id}}

    # 构造 Agent 输入
    if req.decisions:
        # 恢复被中断的流：传 Command(resume=...)
        agent_input: Any = Command(resume={"decisions": req.decisions})
        logger.info(
            "SSE | event=stream_start | thread_id={} | mode=resume | decisions={}",
            thread_id,
            len(req.decisions),
        )
    else:
        # 新对话：必须有 message
        if not req.message:
            raise ValueError("新对话必须提供 message")
        agent_input = {"messages": [{"role": "user", "content": req.message}]}
        logger.info(
            "SSE | event=stream_start | thread_id={} | mode=new | message={}",
            thread_id,
            req.message[:100],
        )

    async def event_generator() -> AsyncIterator[str]:
        """SSE 事件生成器。

        参考 ai_native_invoke_stream 的流式翻译：
        - agent.astream(agent_input, config, stream_mode=["messages","updates"])
        - 流结束后调 aget_state(config).interrupts 判断是否被审批中断
        """
        final_result = ""
        stream_status = "completed"
        producer_task: asyncio.Task | None = None

        try:
            # 发送 agent_state(running)
            yield _sse_event("agent_state", {"thread_id": thread_id, "status": "running"})

            # producer/consumer 模式：producer 独立跑 astream，consumer 并发取队列
            # 超时发心跳保活，避免 docker build 期间 SSE 流静默导致客户端超时
            _queue: asyncio.Queue = asyncio.Queue()
            _SENTINEL = object()
            # 实时构建日志队列：build_docker_image 在 producer task 内通过 ContextVar
            # 拿到同一队列逐行发布，消费者并发排空并转发为 build_log SSE 事件
            _build_queue: asyncio.Queue = asyncio.Queue()
            set_build_log_queue(_build_queue)

            async def _producer():
                """独立消费 astream，把 chunk 放入队列。"""
                try:
                    async for chunk in agent.astream(
                        agent_input,
                        config,
                        stream_mode=["messages", "updates"],
                    ):
                        await _queue.put(chunk)
                except Exception as exc:
                    await _queue.put(("__stream_error__", exc))
                finally:
                    await _queue.put(_SENTINEL)

            producer_task = asyncio.create_task(_producer())

            while True:
                ready = await _await_queue_items(_queue, _build_queue)
                if ready is None:
                    # 心跳保活：SSE 注释行（: 开头），客户端忽略但连接不断
                    yield ": keepalive\n\n"
                    continue

                done_stream = False
                processed = False
                for queue, item in ready:
                    # ---------- 实时构建日志（build_log） ----------
                    if queue is _build_queue:
                        if (
                            isinstance(item, tuple)
                            and len(item) == 3
                            and item[0] == _BUILD_LOG_TAG
                        ):
                            _, tool, line = item
                            if line:
                                yield _sse_event(
                                    "build_log",
                                    {
                                        "thread_id": thread_id,
                                        "tool": tool,
                                        "text": line,
                                    },
                                )
                        continue

                    # ---------- astream 通道 ----------
                    if item is _SENTINEL:
                        done_stream = True
                        break

                    # producer 把 astream 异常传过来了，抛给外层 except 处理
                    if (
                        isinstance(item, tuple)
                        and len(item) == 2
                        and item[0] == "__stream_error__"
                    ):
                        raise item[1]  # noqa: TRY301

                    chunk = item
                    # chunk 格式：(stream_mode_name, payload)
                    # 参考 langgraph stream_mode=["messages","updates"] 的输出
                    if not isinstance(chunk, tuple) or len(chunk) != 2:
                        continue

                    mode, payload = chunk
                    processed = True
                    logger.debug("SSE | event=raw_chunk | thread_id={} | mode={}", thread_id, mode)

                if done_stream:
                    break
                # 本轮只有构建日志、没有 astream chunk：直接等下一轮
                if not processed:
                    continue

                # ---------- messages 模式：LLM 文本增量 + tool_calls ----------
                if mode == "messages":
                    # payload 通常是 (message, metadata)
                    if isinstance(payload, tuple) and len(payload) >= 1:
                        message = payload[0]
                    else:
                        message = payload

                    # 文本增量（只转发 AIMessage 的模型文本）：
                    # ToolMessage 也在 messages 模式里流转，其 content 是工具返回原文
                    # （如 read_file 返回的整份 SKILL.md），若当作文本增量推给前端，
                    # 文件内容会被渲染进 AI 气泡造成刷屏。ToolMessage 的结果
                    # 已由 updates 模式走 tool_call_end 下发，这里跳过。
                    if isinstance(message, AIMessage):
                        text = _extract_text_from_message(message)
                        if text:
                            final_result += text
                            yield _sse_event(
                                "message_delta",
                                {"thread_id": thread_id, "text": text},
                            )

                    # tool_calls（LLM 决定调用工具时）
                    for tc in _extract_tool_calls(message):
                        tool_name = tc.get("name", "")
                        args = tc.get("args", {})
                        call_id = tc.get("id", "")
                        logger.debug(
                            "SSE | event=tool_call_start | thread_id={} | tool={} | call_id={}",
                            thread_id,
                            tool_name,
                            call_id,
                        )
                        yield _sse_event(
                            "tool_call_start",
                            {
                                "thread_id": thread_id,
                                "name": tool_name,
                                "args": args,
                                "call_id": call_id,
                                "hint": TOOL_HINT_MAP.get(tool_name, ""),
                            },
                        )

                # ---------- updates 模式：工具执行结果 ----------
                elif mode == "updates":
                    # updates 通常是 dict，key 是节点名（如 "tools"），value 是状态更新
                    if not isinstance(payload, dict):
                        continue

                    # 遍历各节点更新
                    for node_name, node_state in payload.items():
                        if not isinstance(node_state, dict):
                            continue
                        messages = node_state.get("messages", [])
                        if not isinstance(messages, list):
                            continue

                        for msg in messages:
                            # ToolMessage：工具执行结果
                            if isinstance(msg, ToolMessage):
                                tool_name = msg.name or ""
                                call_id = msg.tool_call_id or ""
                                content = msg.content
                                # content 可能是 JSON 字符串（工具返回的结构化结果）
                                result_data: Any = content
                                if isinstance(content, str):
                                    try:
                                        result_data = json.loads(content)
                                    except (json.JSONDecodeError, ValueError):
                                        result_data = content

                                logger.debug(
                                    "SSE | event=tool_call_end | thread_id={} | tool={} | call_id={}",
                                    thread_id,
                                    tool_name,
                                    call_id,
                                )
                                yield _sse_event(
                                    "tool_call_end",
                                    {
                                        "thread_id": thread_id,
                                        "name": tool_name,
                                        "result": result_data,
                                        "call_id": call_id,
                                    },
                                )

                                # task_status 事件
                                status = TOOL_STATUS_MAP.get(tool_name)
                                if status:
                                    yield _sse_event(
                                        "task_status",
                                        {
                                            "thread_id": thread_id,
                                            "status": status,
                                            "tool": tool_name,
                                        },
                                    )

                                # log 事件：把工具返回的 log 字段切成多条
                                # build_docker_image 的构建过程已通过 build_log
                                # 实时推送，这里跳过整块 log，避免日志区重复堆积
                                log_text = ""
                                if isinstance(result_data, dict):
                                    log_text = result_data.get("log", "") or ""
                                if log_text and tool_name != "build_docker_image":
                                    for chunk_text in _split_log_to_chunks(log_text):
                                        yield _sse_event(
                                            "log",
                                            {
                                                "thread_id": thread_id,
                                                "tool": tool_name,
                                                "text": chunk_text,
                                            },
                                        )

                if done_stream:
                    break

        except Exception as exc:
            # 流异常：发 error 事件
            stream_status = "error"
            logger.error(
                "SSE | event=stream_error | thread_id={} | error={}",
                thread_id,
                str(exc),
                exc_info=True,
            )
            yield _sse_event(
                "error",
                {"thread_id": thread_id, "message": str(exc)},
            )
            return
        finally:
            # 清理构建日志队列的 ContextVar，防止泄漏到下一个请求
            set_build_log_queue(None)
            # 确保 producer task 被清理（客户端断开或异常时 cancel，避免泄漏）
            if producer_task and not producer_task.done():
                producer_task.cancel()
                try:
                    await producer_task
                except (asyncio.CancelledError, Exception):
                    pass

        # ---------- 流结束：检查是否被审批中断 ----------
        try:
            state = await agent.aget_state(config)
            interrupts = getattr(state, "interrupts", None) or []
            if interrupts:
                # 被审批中断：发 approval_required 并结束
                stream_status = "awaiting_approval"
                logger.info(
                    "SSE | event=stream_end | thread_id={} | status=awaiting_approval",
                    thread_id,
                )

                # 提取第一个 interrupt 的 action_requests
                interrupt_obj = interrupts[0]
                approval_data = _build_action_request_from_interrupt(interrupt_obj)
                approval_data["thread_id"] = thread_id

                logger.info(
                    "SSE | event=approval_required | thread_id={} | actions={}",
                    thread_id,
                    len(approval_data.get("action_requests", [])),
                )
                yield _sse_event("approval_required", approval_data)

                # agent_state 变为 awaiting_approval
                yield _sse_event(
                    "agent_state",
                    {"thread_id": thread_id, "status": "awaiting_approval"},
                )
                return
        except Exception as exc:
            # aget_state 失败不影响主流程，记录日志
            logger.warning(
                "SSE | aget_state_failed | thread_id={} | error={}",
                thread_id,
                str(exc),
            )

        # 正常结束：发 stream_complete
        logger.info(
            "SSE | event=stream_end | thread_id={} | status=completed",
            thread_id,
        )
        yield _sse_event(
            "stream_complete",
            {"thread_id": thread_id, "final_result": final_result},
        )
        yield _sse_event(
            "agent_state",
            {"thread_id": thread_id, "status": "completed"},
        )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
        },
    )
