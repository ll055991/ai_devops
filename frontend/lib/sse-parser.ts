// ---------------------------------------------------------------------------
// SSE 解析器（任务 2.2）
//
// 职责：从 fetch 返回的 ReadableStream 逐块解码，按 \n\n 切分事件块，
//       解析 event:/data: 行，回调 onEvent(event)。
//
// 为什么不用 EventSource：
//   浏览器原生 EventSource 只能发 GET 且不能带 JSON body，
//   本项目 POST /api/agent/chat 需要带 {message, thread_id, decisions}，
//   所以必须用 fetch + ReadableStream 手动按 \n\n 切分。
//
// 设计要点（参考前端规划方案 5.2 节）：
//   1. TextDecoder 增量解码 UTF-8 字节，维护 buffer 累积未完成部分
//   2. 按 \n\n 切完整事件块；不完整的尾部留在 buffer 等下一块
//   3. : keepalive 等 SSE 注释行直接跳过（SSE 规范）
//   4. 单行解析失败 console.warn 并继续，不抛错（任务模板「不得中断」要求）
//   5. 不做自动重连（任务 3.2 明确）
// ---------------------------------------------------------------------------

import type { SseEvent } from "./types";

// 后端 event: 名 → 前端 type 名的映射表
// 两者目前同名，但保留显式映射表更安全：
//   后端若误发未登记的事件，会被过滤掉而不会污染 SseEvent 联合类型
const EVENT_TYPE_MAP: Record<string, SseEvent["type"]> = {
  agent_state: "agent_state",
  message_delta: "message_delta",
  tool_call_start: "tool_call_start",
  tool_call_end: "tool_call_end",
  task_status: "task_status",
  log: "log",
  build_log: "build_log",
  approval_required: "approval_required",
  stream_complete: "stream_complete",
  error: "error",
};

// 解析单条 SSE 事件块（已按 \n\n 切出来的多行字符串）
// 解析失败返回 null，调用方继续处理下一条，不抛错
function parseEventBlock(block: string): SseEvent | null {
  let eventType = "";
  let dataLine = "";

  for (const rawLine of block.split("\n")) {
    const line = rawLine.trimEnd();
    if (!line) continue;

    // SSE 注释行（以 : 开头，如 : keepalive）按规范丢弃
    if (line.startsWith(":")) continue;

    if (line.startsWith("event:")) {
      eventType = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLine = line.slice(5).trim();
    } else {
      // 未识别行：记录但不中断流，方便排查后端协议偏差
      console.warn(`sse parse skip line: ${line}`);
    }
  }

  if (!eventType || !dataLine) return null;

  const mappedType = EVENT_TYPE_MAP[eventType];
  if (!mappedType) {
    console.warn(`sse parse skip unknown event: ${eventType}`);
    return null;
  }

  try {
    const payload = JSON.parse(dataLine) as Record<string, unknown>;
    // 注入 type 字段后展开为完整事件对象，调用方按 event.type 收窄
    return { type: mappedType, ...payload } as SseEvent;
  } catch (err) {
    // 仅截前 200 字符避免日志爆炸
    console.warn(`sse parse skip bad json: ${dataLine.slice(0, 200)}`, err);
    return null;
  }
}

// 主入口：从 ReadableStream 增量解析 SSE
// 设计：buffer 累积未完成的字节解码结果，按 \n\n 切完整事件块
// onParseSkip：可选回调，解析器丢弃异常行/未知事件/坏 JSON 时通知调用方
//   （任务 2.3-8：前端把解析异常 console.error + 日志区 [系统] 提示，用于区分
//   前后端问题；不传时保持原有行为，仅 console.warn）
// inactivityTimeoutMs：可选活动超时（毫秒）。超过该时长无任何数据（含心跳）则
//   cancel 流并抛超时错误，防止后端挂起时前端无限等待（审批恢复场景尤其重要）；
//   不传时不做超时检测。后端心跳 15s 一条，传入 60s 不会误杀正常流。
export async function parseSseStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (event: SseEvent) => void,
  onParseSkip?: (reason: string) => void,
  inactivityTimeoutMs?: number,
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  // 活动超时计时器：每次读到数据后重置
  let timeoutHandle: ReturnType<typeof setTimeout> | undefined;
  let timedOut = false;

  const armTimeout = () => {
    if (inactivityTimeoutMs === undefined) return;
    if (timeoutHandle) clearTimeout(timeoutHandle);
    timeoutHandle = setTimeout(() => {
      timedOut = true;
      // cancel 让进行中的 reader.read() 尽快以 done 结束，随后抛超时错误
      void reader.cancel().catch(() => {});
    }, inactivityTimeoutMs);
  };

  try {
    while (true) {
      armTimeout();
      const { done, value } = await reader.read();
      if (done) break;

      // stream: true 表示后续还有数据，避免多字节字符在块边界被截断
      buffer += decoder.decode(value, { stream: true });

      // 按空行切块，最后一个可能不完整，留到 buffer 等下一块
      let sepIndex: number;
      while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);

        const event = parseEventBlock(block);
        if (event) {
          onEvent(event);
        } else if (onParseSkip && isEventLikeBlock(block)) {
          onParseSkip(`丢弃无法解析的事件块: ${block.slice(0, 100)}`);
        }
      }
    }

    // 流结束后处理 buffer 中残留的最后一块（无 \n\n 结尾的边界情况）
    const tail = buffer.trim();
    if (tail) {
      const event = parseEventBlock(tail);
      if (event) {
        onEvent(event);
      } else if (onParseSkip && isEventLikeBlock(tail)) {
        onParseSkip(`丢弃无法解析的尾部块: ${tail.slice(0, 100)}`);
      }
    }

    // 超时触发（cancel 已让 read 结束）：按异常抛给调用方处理
    if (timedOut) {
      throw new Error(`SSE 流 ${inactivityTimeoutMs}ms 无数据（超时）`);
    }
  } finally {
    if (timeoutHandle) clearTimeout(timeoutHandle);
    reader.releaseLock();
  }
}

// 判断事件块是否真的"像事件"（含 event: 或 data: 行）
// 心跳块（: keepalive 注释行）解析返回 null 但属于正常协议，不应上报解析异常
function isEventLikeBlock(block: string): boolean {
  return /^event:/m.test(block) || /^data:/m.test(block);
}

// 导出纯函数用于单元测试（不导出 parseSseStream 依赖的 ReadableStream）
export { parseEventBlock };
