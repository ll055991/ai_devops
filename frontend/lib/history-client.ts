// ---------------------------------------------------------------------------
// 后端记忆同步客户端（方案 B：后端权威历史）
//
// 职责：调用 BFF 代理（/api/threads、/api/threads/[id]/messages）拉取后端
//       InMemorySaver 里的会话线程，并转换成前端 Message 结构。
// 调用方：app/page.tsx 启动时同步合并到本地会话列表。
// ---------------------------------------------------------------------------

import type { Message } from "@/components/MessageList";

// 后端线程列表项（对齐 backend api.py list_threads 返回结构）
export interface BackendThread {
  thread_id: string;
  updated_at: string;
  message_count: number;
  title: string;
}

// 后端单条消息（只含 user/assistant 文本，ToolMessage 已被后端过滤）
export interface BackendMessage {
  role: "user" | "assistant";
  content: string;
}

// 后端线程详情（对齐 backend api.py get_thread_messages 返回结构）
export interface BackendThreadDetail {
  success: boolean;
  thread_id: string;
  messages: BackendMessage[];
}

// 拉取后端全部线程列表；失败抛错（由调用方记录日志/提示）
export async function fetchBackendThreads(): Promise<BackendThread[]> {
  const res = await fetch("/api/threads", { method: "GET" });
  const data = (await res.json().catch(() => null)) as
    | { success?: boolean; threads?: BackendThread[]; error?: string }
    | null;
  if (!res.ok || !data || data.success === false) {
    throw new Error(data?.error ?? `HTTP ${res.status}`);
  }
  return data.threads ?? [];
}

// 后端线程不存在（404）：后端重启后旧 threadId 已失去记忆，前端据此降级为新对话
export class ThreadNotFoundError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ThreadNotFoundError";
  }
}

// 拉取单个线程的消息历史；线程不存在（404）抛 ThreadNotFoundError
export async function fetchBackendThreadMessages(
  threadId: string,
): Promise<BackendMessage[]> {
  const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}/messages`, {
    method: "GET",
  });
  const data = (await res.json().catch(() => null)) as
    | { success?: boolean; messages?: BackendMessage[]; error?: string }
    | null;
  if (res.status === 404) {
    throw new ThreadNotFoundError("线程不存在（后端已重启，该会话记忆已丢失）");
  }
  if (!res.ok || !data || data.success === false) {
    throw new Error(data?.error ?? `HTTP ${res.status}`);
  }
  return data.messages ?? [];
}

// 后端消息 → 前端 Message（过滤空文本；id 用 bk- 前缀避免与本地 id-N 冲突）
export function toFrontendMessages(
  msgs: BackendMessage[],
  threadId: string,
): Message[] {
  return msgs
    .filter((m) => m.content.length > 0)
    .map((m, i) => ({
      id: `bk-${threadId}-${i}`,
      role: m.role,
      content: m.content,
      streaming: false,
    }));
}

// 后端时间戳（ISO 字符串）→ 毫秒时间戳；解析失败退回当前时间
export function toTimestamp(iso: string): number {
  const ts = Date.parse(iso);
  return Number.isFinite(ts) ? ts : Date.now();
}

// 删除后端线程的检查点数据（SQLite 物理删除）；失败不抛错，仅记录日志
// 调用方：page.tsx 删除对话时同步删除后端数据
export async function deleteBackendThread(threadId: string): Promise<boolean> {
  try {
    const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}`, {
      method: "DELETE",
    });
    const data = (await res.json().catch(() => null)) as
      | { success?: boolean; error?: string }
      | null;
    if (!res.ok || !data || data.success === false) {
      console.error("[history-client] delete backend thread failed", {
        threadId,
        error: data?.error ?? `HTTP ${res.status}`,
      });
      return false;
    }
    return true;
  } catch (err) {
    console.error("[history-client] delete backend thread failed", {
      threadId,
      err: err instanceof Error ? err.message : String(err),
    });
    return false;
  }
}