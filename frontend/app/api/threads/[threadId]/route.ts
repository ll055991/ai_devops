// ---------------------------------------------------------------------------
// BFF 代理 —— DELETE /api/threads/[threadId]
//   → 后端 DELETE /api/agent/threads/{thread_id}
//
// 职责：前端删除对话时同步删除后端 SQLite 检查点数据，
//       确保重启后端后已删会话不再被 mergeBackendThreads 拉回。
// ---------------------------------------------------------------------------

import { createLogger } from "@/lib/logger";

const log = createLogger("bff-thread-delete");

const BACKEND_URL = process.env.AGENT_API_URL ?? "http://127.0.0.1:8000";

function errorResponse(status: number, message: string): Response {
  return new Response(JSON.stringify({ success: false, error: message }), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

export async function DELETE(
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
      `${BACKEND_URL}/api/agent/threads/${encodeURIComponent(threadId)}`,
      {
        method: "DELETE",
        headers: { Accept: "application/json" },
        signal: AbortSignal.timeout(10000),
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

  if (!backendRes.ok || data.success === false) {
    log.error("backend error", { status: backendRes.status, threadId, error: data.error });
    return errorResponse(backendRes.ok ? 500 : backendRes.status, data.error ?? "后端未知错误");
  }

  log.debug("thread deleted", { threadId, deleted: data.deleted });
  return Response.json(data);
}
