// ---------------------------------------------------------------------------
// BFF 代理 —— GET /api/threads/[threadId]/messages
//   → 后端 GET /api/agent/threads/{thread_id}/messages
//
// 记忆机制（方案 B）：从后端拉取单个线程的完整消息历史（权威源），
// 前端据此恢复会话上下文，多端/换浏览器也能找回对话。
//
// 错误处理：所有失败转结构化 JSON { success: false, error }（约束 2）；
// 后端 404（线程不存在）原样透传，前端按"无此线程"处理。
// ---------------------------------------------------------------------------

import { createLogger } from "@/lib/logger";

const log = createLogger("bff-thread-messages");

const BACKEND_URL = process.env.AGENT_API_URL ?? "http://127.0.0.1:8000";

function errorResponse(status: number, message: string): Response {
  return new Response(JSON.stringify({ success: false, error: message }), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

// Next 16：动态路由参数为 Promise，需 await
export async function GET(
  _req: Request,
  context: { params: Promise<{ threadId: string }> },
) {
  const { threadId } = await context.params;
  // 参数校验：空/含路径分隔符的 threadId 直接拒绝
  if (!threadId || threadId.includes("/") || threadId.includes("\\")) {
    log.warn("invalid threadId", { threadId });
    return errorResponse(400, "非法的 threadId");
  }

  let backendRes: Response;
  try {
    backendRes = await fetch(
      `${BACKEND_URL}/api/agent/threads/${encodeURIComponent(threadId)}/messages`,
      {
        method: "GET",
        headers: { Accept: "application/json" },
        signal: AbortSignal.timeout(5000),
      },
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    log.error("backend fetch failed", { err: msg, threadId, backend: BACKEND_URL });
    return errorResponse(502, `无法连接后端服务 (${BACKEND_URL})：${msg}`);
  }

  const data = await backendRes.json().catch(() => null);
  if (data === null) {
    log.error("backend non-json", { status: backendRes.status, threadId });
    return errorResponse(502, "后端返回了无法解析的响应");
  }

  // 后端 404：线程不存在，按结构化错误透传
  if (backendRes.status === 404) {
    log.debug("thread not found on backend", { threadId });
    return Response.json(data, { status: 404 });
  }
  if (!backendRes.ok || data.success === false) {
    log.error("backend error", { status: backendRes.status, threadId, error: data.error });
    return errorResponse(backendRes.ok ? 500 : backendRes.status, data.error ?? "后端未知错误");
  }

  log.debug("thread messages fetched", { threadId, count: data.messages?.length ?? 0 });
  return Response.json(data);
}