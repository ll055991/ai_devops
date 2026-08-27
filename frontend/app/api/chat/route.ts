// ---------------------------------------------------------------------------
// BFF 代理路由 —— 前端 → POST /api/chat → 后端 /api/agent/chat
//
// 参考 ai-native/app/api/ontology-chat/route.ts 的透传思路，但按本项目契约大幅精简：
//   - 砍掉 SQLite 持久化（任务模板要求 Demo 不做线程映射）
//   - 砍掉 interceptor（不做 metadata 拦截）
//   - 砍掉 convertSSEStreamToUIChunks（不用 @assistant-ui，前端直接消费后端 9 种事件）
//   - 砍掉 workspace_id 注入（本项目后端不需要）
//
// 决策 2：BFF 直连后端 AGENT_API_URL（默认 8000），不引入 local-gateway.mjs 网关。
//
// 字段映射：前端驼峰（threadId）→ 后端下划线（thread_id）
// 错误处理：所有失败转结构化 JSON 返回，不让 Agent 崩溃（约束 2）
// ---------------------------------------------------------------------------

import { createLogger } from "@/lib/logger";

const log = createLogger("bff-chat");

// 后端服务地址：dev 从 .env.development 读取，默认 8000（决策 2）
const BACKEND_URL = process.env.AGENT_API_URL ?? "http://127.0.0.1:8000";
const BACKEND_CHAT_URL = `${BACKEND_URL}/api/agent/chat`;

// 前端请求体（驼峰命名，前端组件用）
interface FrontendRequestBody {
  message?: string;
  threadId?: string;
  decisions?: Array<{ type: "approve" | "reject"; message?: string }>;
}

// 驼峰 → 下划线转换：后端 ChatRequest 用下划线（api.py 的 Pydantic 模型）
// 只透传存在的字段，避免把 undefined 序列化成 null 干扰后端校验
function toBackendBody(body: FrontendRequestBody): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (body.message !== undefined) out.message = body.message;
  if (body.threadId !== undefined) out.thread_id = body.threadId;
  if (body.decisions !== undefined) out.decisions = body.decisions;
  return out;
}

// 结构化错误响应（参考 ai-native createErrorResponse 思路）
// 统一 { success: false, error } 格式，前端按此结构识别
function errorResponse(status: number, message: string): Response {
  return new Response(JSON.stringify({ success: false, error: message }), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

// POST /api/chat —— 主流程：新对话 / 审批恢复 / 流式响应
export async function POST(req: Request) {
  // 1. 解析前端请求体，JSON 格式错误直接 400
  let body: FrontendRequestBody;
  try {
    body = (await req.json()) as FrontendRequestBody;
  } catch {
    log.warn("request body not valid JSON");
    return errorResponse(400, "请求格式错误：无法解析 JSON");
  }

  // 2. 参数校验（对齐后端 api.py 规则，提前拦截避免无效转发）
  //    后端规则：message 可选，但新对话（无 thread_id）必须有 message；审批恢复必须带 thread_id
  const hasDecisions = Array.isArray(body.decisions) && body.decisions.length > 0;
  if (!body.threadId && !body.message) {
    log.warn("new conversation missing message", { hasThreadId: !!body.threadId, hasMessage: !!body.message });
    return errorResponse(400, "新会话缺少 message 字段");
  }
  if (!body.threadId && hasDecisions) {
    log.warn("approval resume missing threadId");
    return errorResponse(400, "审批恢复必须提供 threadId");
  }

  // 3. 转发到后端 /api/agent/chat
  const backendBody = toBackendBody(body);
  log.info("forward to backend", {
    threadId: body.threadId,
    hasMessage: !!body.message,
    hasDecisions,
    backend: BACKEND_CHAT_URL,
  });

  let backendRes: Response;
  try {
    backendRes = await fetch(BACKEND_CHAT_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no", // 防止反代缓冲 SSE
      },
      body: JSON.stringify(backendBody),
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    log.error("backend fetch failed", { err: msg, backend: BACKEND_CHAT_URL });
    return errorResponse(502, `无法连接后端服务 (${BACKEND_URL})：${msg}`);
  }

  // 4. 后端非 2xx：转结构化错误，附带后端原始文本便于排查
  if (!backendRes.ok) {
    const errorText = await backendRes.text().catch(() => "Unknown error");
    log.error("backend non-2xx", { status: backendRes.status, errorText });
    return errorResponse(backendRes.status, `后端返回错误: HTTP ${backendRes.status} ${errorText}`);
  }

  if (!backendRes.body) {
    log.error("backend returned empty body");
    return errorResponse(502, "后端返回了空响应体");
  }

  // 5. 透传 SSE 流：直接把后端 ReadableStream 作为新 Response 的 body
  //    不做任何事件转换（前端任务 2.2 的 sse-parser 直接消费原始事件）
  //    参考 ai-native route.ts 的 Response 包装，但砍掉 ReadableStream 里的 chunk 转换
  log.debug("streaming backend SSE to client", { status: backendRes.status });
  return new Response(backendRes.body, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      "X-Accel-Buffering": "no",
      Connection: "keep-alive",
    },
  });
}

// GET /api/chat —— 健康检查：探活后端 /healthz，联调时方便定位是前端还是后端的问题
export async function GET() {
  try {
    const res = await fetch(`${BACKEND_URL}/healthz`, {
      method: "GET",
      signal: AbortSignal.timeout(5000),
    });
    const data = await res.json().catch(() => ({}));
    log.info("backend health ok", { status: res.status });
    return Response.json({ backend: BACKEND_URL, status: res.status, backendHealth: data });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    log.error("backend health check failed", { err: msg });
    return Response.json(
      { backend: BACKEND_URL, status: "unreachable", error: msg },
      { status: 502 },
    );
  }
}
