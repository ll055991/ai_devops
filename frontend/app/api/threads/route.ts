// ---------------------------------------------------------------------------
// BFF 代理 —— GET /api/threads → 后端 GET /api/agent/threads
//
// 记忆机制（方案 B）：后端权威历史。前端启动时经此接口拉取后端内存中的
// 全部会话线程列表（thread_id / 更新时间 / 消息数 / 标题），
// 用于恢复/合并前端 localStorage 里的历史会话。
//
// 错误处理：所有失败转结构化 JSON { success: false, error }（约束 2）。
// ---------------------------------------------------------------------------

import { createLogger } from "@/lib/logger";

const log = createLogger("bff-threads");

const BACKEND_URL = process.env.AGENT_API_URL ?? "http://127.0.0.1:8000";
const BACKEND_THREADS_URL = `${BACKEND_URL}/api/agent/threads`;

function errorResponse(status: number, message: string): Response {
  return new Response(JSON.stringify({ success: false, error: message }), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

export async function GET() {
  let backendRes: Response;
  try {
    backendRes = await fetch(BACKEND_THREADS_URL, {
      method: "GET",
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(5000),
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    log.error("backend fetch failed", { err: msg, backend: BACKEND_URL });
    return errorResponse(502, `无法连接后端服务 (${BACKEND_URL})：${msg}`);
  }

  const data = await backendRes.json().catch(() => null);
  if (!backendRes.ok || data === null) {
    log.error("backend non-2xx or bad json", { status: backendRes.status });
    return errorResponse(502, `后端返回错误: HTTP ${backendRes.status}`);
  }

  // 后端 {success:false} 结构化错误原样透传状态码
  if (data.success === false) {
    log.error("backend returned error", { error: data.error });
    return errorResponse(500, data.error ?? "后端未知错误");
  }

  log.debug("threads fetched", { count: data.threads?.length ?? 0 });
  return Response.json(data);
}