// ---------------------------------------------------------------------------
// 前端 API 客户端（任务 2.3）
//
// 封装 POST /api/chat + SSE 流消费 + abort，供 page.tsx 的 useReducer 调用。
// 设计要点：
//   - fetch 带 AbortController，用户可取消请求
//   - res.body 喂给 parseSseStream 逐事件回调 onEvent
//   - 流结束但未收到终态事件（stream_complete/error/approval_required）时调
//     onStreamClose（断流提示）
//   - fetch 本身失败时调 onStreamError
//   - 请求体保持驼峰发给 BFF；字段映射（threadId → thread_id）由 BFF
//     route.ts 统一负责，此处不得提前转换（否则 BFF 读不到 threadId 会丢字段）
// ---------------------------------------------------------------------------

import { parseSseStream } from "./sse-parser";
import type { SseEvent } from "./types";
import { createLogger } from "./logger";

const log = createLogger("api-client");

// 活动超时：超过该时长无任何数据（含心跳）则判定断流，触发 onStreamError
// 后端心跳间隔 15s（api.py _HEARTBEAT_INTERVAL），60s 足够覆盖且不会误杀正常流
const INACTIVITY_TIMEOUT_MS = 60_000;

// 前端请求体（驼峰，组件用）
export interface ChatRequestBody {
  message?: string;
  threadId?: string;
  decisions?: Array<{ type: "approve" | "reject"; message?: string }>;
}

// 调用选项
export interface PostChatOptions {
  /** 每个解析出的 SSE 事件回调 */
  onEvent: (event: SseEvent) => void;
  /** fetch 本身失败（网络错误/非 2xx）回调 */
  onStreamError?: (err: Error) => void;
  /** 流结束但未收到 stream_complete/error 回调（断流提示） */
  onStreamClose?: () => void;
  /** SSE 解析器丢弃异常块回调（任务 2.3-8：前端日志区 [系统] 提示） */
  onParseSkip?: (reason: string) => void;
}

// 返回控制句柄：abort 供用户取消
export interface PostChatHandle {
  abort: () => void;
}

// 请求体序列化：保持驼峰原样发给 BFF（/api/chat）
// 字段转换（threadId → thread_id）由 BFF route.ts 统一负责，这里不得提前转换，
// 否则 BFF 读不到 body.threadId 会在转发时丢弃该字段（导致每轮都开新线程）
function serializeBody(body: ChatRequestBody): string {
  // JSON.stringify 天然跳过值为 undefined 的字段
  return JSON.stringify(body);
}

// 主入口：发 POST /api/chat 并消费 SSE 流
export function postChat(
  body: ChatRequestBody,
  options: PostChatOptions,
): PostChatHandle {
  const controller = new AbortController();
  let receivedTerminal = false;

  (async () => {
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: serializeBody(body),
        signal: controller.signal,
      });

      if (!res.ok) {
        const text = await res.text().catch(() => "Unknown error");
        options.onStreamError?.(new Error(`HTTP ${res.status}: ${text}`));
        return;
      }

      if (!res.body) {
        options.onStreamError?.(new Error("后端返回了空响应体"));
        return;
      }

      await parseSseStream(
        res.body,
        (event) => {
          // 标记是否收到终态事件
          // approval_required 也视为终态：后端发完该事件后会结束本次流，等待人工审批
          //（不会补发 stream_complete/error，若不标记会被误判为断流 → 弹「连接中断」）
          if (
            event.type === "stream_complete" ||
            event.type === "error" ||
            event.type === "approval_required"
          ) {
            receivedTerminal = true;
          }
          options.onEvent(event);
        },
        options.onParseSkip,
        // 60s 无任何数据（含心跳）视为断流，避免审批恢复等场景无限等待
        INACTIVITY_TIMEOUT_MS,
      );

      // 流结束但未收到终态事件 → 断流提示（任务 3.2 要求）
      if (!receivedTerminal) {
        log.warn("stream ended without terminal event");
        options.onStreamClose?.();
      }
    } catch (err) {
      // AbortError 是用户主动取消，不算异常
      if (err instanceof DOMException && err.name === "AbortError") {
        log.debug("request aborted by user");
        return;
      }
      const msg = err instanceof Error ? err.message : String(err);
      log.error("fetch failed", { err: msg });
      options.onStreamError?.(err instanceof Error ? err : new Error(msg));
    }
  })();

  return {
    abort: () => {
      controller.abort();
    },
  };
}
