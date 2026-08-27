// ---------------------------------------------------------------------------
// 对话主页面组件（工具卡片内嵌在 AI 气泡上方；含历史会话侧边栏）
//
// 职责：单页装配各组件（Sidebar / MessageList / ApprovalDialog /
//       TaskStatusBar / Composer），用 useReducer 维护全部状态：
//   - messages      当前会话消息（message_delta 流式渲染，最终文本入 state；
//                   工具调用卡片挂在对应 assistant 消息的 toolCards 字段，
//                   随会话持久化，多轮历史保留）
//   - conversations 历史会话列表（localStorage 持久化，唯一数据源）
//   - activeConvId  当前激活会话 id
//   - 会话切换时 loadConversation 恢复 messages/threadId 多轮上下文
//   - 消息增量/完成/threadId 变动时 reducer 内实时同步到当前会话并写盘
// ---------------------------------------------------------------------------

"use client";

// 引入 React 核心 Hook
import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
// 引入消息列表展示组件
import MessageList, { type Message } from "@/components/MessageList";
// 引入历史会话侧边栏组件
import Sidebar, { type Conversation } from "@/components/Sidebar";
// 引入工具卡片数据类型与工具成功判断辅助函数（卡片渲染在 MessageList 内完成）
import { type ToolCardData, isResultSuccess } from "@/components/ToolCard";
// 引入人工审批确认弹窗组件
import ApprovalDialog, {
  type ApprovalData,
  type ApprovalDecisionType,
} from "@/components/ApprovalDialog";
// 引入任务阶段指示条组件
import TaskStatusBar from "@/components/TaskStatusBar";
// 引入底部消息输入与发送框组件
import Composer from "@/components/Composer";
// 引入后端 API 请求方法与句柄类型
import { postChat, type PostChatHandle } from "@/lib/api-client";
// 引入后端记忆同步客户端（方案 B：后端权威历史；方案 C：续聊前探活）
import {
  deleteBackendThread,
  fetchBackendThreadMessages,
  fetchBackendThreads,
  ThreadNotFoundError,
  toFrontendMessages,
  toTimestamp,
} from "@/lib/history-client";
// 引入 SSE 数据推送事件接口类型
import type { SseEvent } from "@/lib/types";
// 引入前端日志工具
import { createLogger } from "@/lib/logger";
// 引入页面样式文件
import styles from "./page.module.css";

// 初始化页面日志记录器
const log = createLogger("page");

// ---------------------------------------------------------------------------
// 历史会话：localStorage 持久化（浏览器本地存储）
// ---------------------------------------------------------------------------

// localStorage 存储 key（带版本号，格式变更时便于迁移/废弃）
const CONVERSATIONS_STORAGE_KEY = "deploy-agent.conversations.v1";
// 已删除的 threadId 集合（防止后端合并时把已删会话拉回来）
const DELETED_THREADS_STORAGE_KEY = "deploy-agent.deletedThreads.v1";

// 新建对话欢迎态：运维部署场景快捷提示词候选池
const SUGGESTION_POOL = [
  "一键部署：拉取最新代码并构建部署",
  "查询服务器正在运行的容器",
  "列出工作区目录下的文件列表",
  "检查代码目录是否包含 Dockerfile",
  "查看服务器上的 Docker 镜像列表",
  "查询服务健康状态",
  "检查工作区目录结构",
  "读取部署技能文档",
];

// 字符串哈希（FNV-1a），用于从 seed 派生确定性伪随机序列
function hashString(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

// 从 seed（会话 id）确定性抽取 count 个不重复的快捷提示词：
// SSR 与客户端水合阶段使用同一 seed 得到同一结果，避免 hydration mismatch；
// 同一会话固定一组，切换会话重新抽取
function pickSuggestions(count: number, seed: string): string[] {
  const pool = [...SUGGESTION_POOL];
  const picked: string[] = [];
  let h = hashString(seed) || 1;
  for (let i = 0; i < count && pool.length > 0; i++) {
    h = (Math.imul(h, 1664525) + 1013904223) >>> 0;
    const idx = h % pool.length;
    picked.push(pool.splice(idx, 1)[0]);
  }
  return picked;
}

// 生成会话 id
function genConversationId(): string {
  return `c-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// 首条消息自动总结为会话标题：取第一行，超过 20 字截断
function summarizeTitle(text: string): string {
  const line = (text.split(/\r?\n/)[0] ?? "").trim();
  return line.length > 20 ? `${line.slice(0, 20)}…` : line;
}

// 校验会话数据（localStorage 可能损坏/被篡改，非法条目一律过滤）
function isValidConversation(c: unknown): c is Conversation {
  if (typeof c !== "object" || c === null) return false;
  const obj = c as Record<string, unknown>;
  return (
    typeof obj.id === "string" &&
    typeof obj.title === "string" &&
    (typeof obj.threadId === "string" || obj.threadId === null) &&
    Array.isArray(obj.messages) &&
    typeof obj.updatedAt === "number"
  );
}

// 校验消息数据（toolCards 可选：内嵌在 assistant 消息上的工具调用卡片）
function isValidMessage(m: unknown): m is Message {
  if (typeof m !== "object" || m === null) return false;
  const obj = m as Record<string, unknown>;
  return (
    typeof obj.id === "string" &&
    (obj.role === "user" || obj.role === "assistant") &&
    typeof obj.content === "string" &&
    typeof obj.streaming === "boolean" &&
    (obj.toolCards === undefined || Array.isArray(obj.toolCards))
  );
}

// 从 localStorage 读取会话列表；解析失败/数据损坏时返回空数组并记录日志
// 返回的数组按 updatedAt 倒序（最新在前），便于侧边栏直接展示
function loadConversations(): Conversation[] {
  // SSR 阶段没有 window（localStorage），真实数据由客户端挂载后恢复
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(CONVERSATIONS_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const convs = parsed
      .filter(isValidConversation)
      .map((c) => ({
        ...c,
        // 历史消息恢复后不再处于流式状态
        messages: c.messages.filter(isValidMessage).map((m) => ({ ...m, streaming: false })),
      }))
      .sort((a, b) => b.updatedAt - a.updatedAt);
    return convs;
  } catch (err) {
    log.error("load conversations failed", {
      err: err instanceof Error ? err.message : String(err),
    });
    return [];
  }
}

// 从 localStorage 读取已删除的 threadId 集合；解析失败返回空集合
function loadDeletedThreadIds(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(DELETED_THREADS_STORAGE_KEY);
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((v): v is string => typeof v === "string"));
  } catch {
    return new Set();
  }
}

// 定义 Agent 的 5 种生命周期运行状态
type AgentState = "idle" | "running" | "awaiting_approval" | "completed" | "error";

// 定义页面全局状态数据结构
interface ChatState {
  // 历史所有消息数组（包含用户输入与 Agent 流式输出）
  messages: Message[];
  // 历史会话列表（localStorage 持久化，最新在前；作为唯一数据源）
  conversations: Conversation[];
  // 当前激活的会话 id（始终存在，初始化为最近会话或新建空白会话）
  activeConvId: string;
  // 日志行数据（保留在内存中用于排查，不在前端展示）
  logLines: Array<{ id: string; tool: string; text: string }>;
  // 系统错误提示列表
  systemNotes: Array<{ id: string; text: string }>;
  // 自增 ID 序列号，用于生成唯一的 key
  idSeq: number;
  // 当前审批请求数据对象
  approval: ApprovalData | null;
  // 审批提交后的等待状态标志
  approvalWaiting: boolean;
  // 当前任务执行阶段名称
  taskStatus: string;
  // Agent 当前运行状态
  agentState: AgentState;
  // 当前会话线程 ID，用于多轮对话上下文关联
  threadId: string | null;
  // 当前是否处于请求发送中
  sending: boolean;
  // 各工具最近一次调用的成功/失败记录表
  toolLastOk: Record<string, boolean>;
  // 异常报错信息文本
  errorMessage: string | null;
  // 已删除的 threadId 集合：防止后端合并时把已删会话拉回来
  deletedThreadIds: Set<string>;
}

// 初始页面状态数据
const initialState: ChatState = {
  messages: [],
  conversations: [],
  activeConvId: "",
  logLines: [],
  systemNotes: [],
  idSeq: 1,
  approval: null,
  approvalWaiting: false,
  taskStatus: "INIT",
  agentState: "idle",
  threadId: null,
  sending: false,
  toolLastOk: {},
  errorMessage: null,
  deletedThreadIds: new Set(),
};

// 定义 Reducer 接收的所有 Action 类型
type ChatAction =
  // 挂载后从 localStorage 恢复会话列表（幂等；blankId/now 由调用方生成）
  | { type: "restoreConversations"; conversations: Conversation[]; deletedThreadIds: Set<string>; blankId: string; now: number }
  // 合并后端权威历史（方案 B）：threadId 已存在且本地更旧时以后端为准
  | { type: "mergeBackendThreads"; conversations: Conversation[] }
  // 新建一条空白会话并置为当前活跃项（id/now 由调用方生成，保持 reducer 纯函数）
  | { type: "createConversation"; id: string; now: number }
  // 切换/载入历史会话（从 state.conversations 内查找）
  | { type: "loadConversation"; id: string }
  // 删除会话；全部删完时新建空白会话（newId/now 由调用方生成）
  | { type: "deleteConversation"; id: string; newId: string; now: number }
  | { type: "sendStart" }
  | { type: "userMessage"; text: string; now: number }
  | { type: "assistantDelta"; text: string; now: number }
  | { type: "assistantFinal"; text: string; now: number }
  | {
      type: "toolStart";
      name: string;
      args: Record<string, unknown>;
      callId: string;
      hint: string;
      now: number;
    }
  | { type: "toolEnd"; name: string; callId: string; result: unknown; now: number }
  | { type: "taskStatus"; status: string }
  | { type: "appendLog"; tool: string; text: string }
  | { type: "appendSystemNote"; text: string }
  | { type: "clearLogs" }
  | { type: "approvalRequired"; data: ApprovalData }
  | { type: "approvalSubmitting" }
  | { type: "approvalResolved" }
  | { type: "setThreadId"; threadId: string; now: number }
  // 清除当前会话的 threadId（方案 C：后端记忆丢失时降级为新对话）
  | { type: "clearThreadId"; now: number }
  | { type: "agentState"; status: AgentState }
  | { type: "rejectedLocally" }
  | { type: "streamComplete"; now: number }
  | { type: "runFailed"; message: string; now: number }
  // 用户主动中止 SSE 请求
  | { type: "userAbort"; now: number };

// 辅助函数：将仍在流式输出状态的气泡关闭光标指示器
function closeStreamingBubbles(messages: Message[]): Message[] {
  return messages.map((m) => (m.streaming ? { ...m, streaming: false } : m));
}

// 视图状态重置（切换/新建/删除会话时复用；不触碰 messages/threadId/会话列表）
function resetView(): Partial<ChatState> {
  return {
    approval: null,
    approvalWaiting: false,
    taskStatus: "INIT",
    agentState: "idle",
    sending: false,
    toolLastOk: {},
    errorMessage: null,
  };
}

// 在 reducer 内把当前会话的消息副本与 state.messages 保持同步
// （消息增量/完成等事件到达时实时更新，便于持久化与侧边栏展示）
function syncActiveConversation(
  state: ChatState,
  messages: Message[],
  now: number,
  extra?: Partial<Conversation>,
): Conversation[] {
  return state.conversations.map((c) =>
    c.id === state.activeConvId ? { ...c, messages, updatedAt: now, ...extra } : c,
  );
}

// 计算历史会话中消息 id 的最大序号 + 1（用于 idSeq 续接，防止 key 冲突）
function nextIdSeq(conversations: Conversation[]): number {
  let max = 0;
  for (const c of conversations) {
    for (const m of c.messages) {
      const n = Number(m.id.replace(/^id-/, ""));
      if (Number.isFinite(n) && n > max) max = n;
    }
  }
  return max + 1;
}

// 页面全局状态管理 Reducer
function reducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    // 挂载后恢复会话列表（幂等：已恢复过则跳过，防 StrictMode 双执行重复恢复）
    case "restoreConversations": {
      if (state.conversations.length > 0) return state;
      if (action.conversations.length > 0) {
        const first = action.conversations[0];
        return {
          ...state,
          conversations: action.conversations,
          activeConvId: first.id,
          messages: first.messages.map((m) => ({ ...m, streaming: false })),
          threadId: first.threadId,
          deletedThreadIds: action.deletedThreadIds,
          // idSeq 从历史消息的最大 id 续接，避免恢复后新消息 key 冲突
          idSeq: nextIdSeq(action.conversations),
          ...resetView(),
        };
      }
      // 无历史：创建空白会话（id/now 由 action 携带，保持纯函数）
      const blank: Conversation = {
        id: action.blankId,
        title: "新对话",
        threadId: null,
        messages: [],
        updatedAt: action.now,
      };
      return {
        ...state,
        conversations: [blank],
        activeConvId: blank.id,
        deletedThreadIds: action.deletedThreadIds,
        ...resetView(),
      };
    }

    // 合并后端权威历史（方案 B）：threadId 已存在且本地消息更少时以后端为准；
    // 若当前活跃会话被更新，同步刷新视图消息；
    // 跳过已删除的 threadId（防止刷新后端合并拉回已删会话）
    case "mergeBackendThreads": {
      let result = state.conversations;
      let updatedActiveMessages: Message[] | null = null;
      for (const conv of action.conversations) {
        // 已删除的线程不合并（用户主动删除，不应被后端数据恢复）
        if (conv.threadId && state.deletedThreadIds.has(conv.threadId)) continue;
        const idx = result.findIndex((c) => c.threadId === conv.threadId);
        if (idx === -1) {
          result = [...result, conv];
        } else if (conv.messages.length > result[idx].messages.length) {
          const merged: Conversation = {
            ...result[idx],
            messages: conv.messages,
            updatedAt: conv.updatedAt,
          };
          result = [...result.slice(0, idx), merged, ...result.slice(idx + 1)];
          if (merged.id === state.activeConvId) {
            updatedActiveMessages = conv.messages;
          }
        }
      }
      if (result === state.conversations) return state;
      // 最新在前
      const sorted = [...result].sort((a, b) => b.updatedAt - a.updatedAt);
      return {
        ...state,
        conversations: sorted,
        ...(updatedActiveMessages ? { messages: updatedActiveMessages } : {}),
      };
    }

    // 新建空白会话并置为当前活跃项
    case "createConversation": {
      const blank: Conversation = {
        id: action.id,
        title: "新对话",
        threadId: null,
        messages: [],
        updatedAt: action.now,
      };
      return {
        ...state,
        conversations: [blank, ...state.conversations],
        activeConvId: action.id,
        messages: [],
        threadId: null,
        ...resetView(),
      };
    }

    // 载入历史会话：恢复 messages/threadId 上下文，其余视图状态重置
    case "loadConversation": {
      const conv = state.conversations.find((c) => c.id === action.id);
      if (!conv) return state;
      const messages = conv.messages.map((m) => ({ ...m, streaming: false }));
      return {
        ...state,
        activeConvId: action.id,
        messages,
        threadId: conv.threadId,
        // 内存副本与存储副本对齐（不刷新 updatedAt，避免切换会话即置顶）
        conversations: state.conversations.map((c) =>
          c.id === action.id ? { ...c, messages } : c,
        ),
        ...resetView(),
      };
    }

    // 删除会话；删除的是当前会话时切换到剩余最近一条，全删完则新建空白会话；
    // 同时将被删会话的 threadId 加入 deletedThreadIds，防止后端合并拉回
    case "deleteConversation": {
      const deleted = state.conversations.find((c) => c.id === action.id);
      const newDeleted = new Set(state.deletedThreadIds);
      if (deleted?.threadId) newDeleted.add(deleted.threadId);
      const next = state.conversations.filter((c) => c.id !== action.id);
      // 删除的是非当前会话：仅从列表移除
      if (action.id !== state.activeConvId) {
        return { ...state, conversations: next, deletedThreadIds: newDeleted };
      }
      if (next.length > 0) {
        const target = next[0];
        const messages = target.messages.map((m) => ({ ...m, streaming: false }));
        return {
          ...state,
          conversations: next,
          activeConvId: target.id,
          messages,
          threadId: target.threadId,
          deletedThreadIds: newDeleted,
          ...resetView(),
        };
      }
      // 全部删完：新建空白会话（id/now 由 action 携带，保持纯函数）
      const blank: Conversation = {
        id: action.newId,
        title: "新对话",
        threadId: null,
        messages: [],
        updatedAt: action.now,
      };
      return {
        ...state,
        conversations: [blank],
        activeConvId: blank.id,
        messages: [],
        threadId: null,
        deletedThreadIds: newDeleted,
        ...resetView(),
      };
    }

    // 发送开始：设置 sending 状态为 true 防止重复提交
    case "sendStart":
      return { ...state, sending: true };

    // 追加用户消息到列表中；首条消息自动总结为会话标题
    case "userMessage": {
      const isFirstMessage = state.messages.length === 0;
      const newMessage: Message = {
        id: `id-${state.idSeq}`,
        role: "user",
        content: action.text,
        streaming: false,
      };
      const newMessages: Message[] = [...state.messages, newMessage];
      // 仅首条消息时更新标题（避免 undefined 覆盖已有标题）
      const titleExtra = isFirstMessage ? { title: summarizeTitle(action.text) } : {};
      return {
        ...state,
        idSeq: state.idSeq + 1,
        messages: newMessages,
        conversations: syncActiveConversation(state, newMessages, action.now, titleExtra),
      };
    }

    // 追加 Agent 正在打字输出的流式增量文本
    case "assistantDelta": {
      const messages = [...state.messages];
      const last = messages[messages.length - 1];
      // 如果上一条消息就是处于流式状态的 assistant 消息，则直接拼接文字
      if (last && last.role === "assistant" && last.streaming) {
        messages[messages.length - 1] = {
          ...last,
          content: last.content + action.text,
        };
        return {
          ...state,
          messages,
          conversations: syncActiveConversation(state, messages, action.now),
        };
      }
      // 否则在末尾新建一个 assistant 消息气泡
      messages.push({
        id: `id-${state.idSeq}`,
        role: "assistant",
        content: action.text,
        streaming: true,
      });
      return {
        ...state,
        idSeq: state.idSeq + 1,
        messages,
        conversations: syncActiveConversation(state, messages, action.now),
      };
    }

    // 流式输出完毕：将权威的最终文本写入最后一条气泡
    case "assistantFinal": {
      const messages = [...state.messages];
      const last = messages[messages.length - 1];
      if (last && last.role === "assistant") {
        messages[messages.length - 1] = {
          ...last,
          content: action.text !== "" ? action.text : last.content,
        };
        return {
          ...state,
          messages,
          conversations: syncActiveConversation(state, messages, action.now),
        };
      }
      if (action.text !== "") {
        messages.push({
          id: `id-${state.idSeq}`,
          role: "assistant",
          content: action.text,
          streaming: false,
        });
        return {
          ...state,
          idSeq: state.idSeq + 1,
          messages,
          conversations: syncActiveConversation(state, messages, action.now),
        };
      }
      return { ...state, messages };
    }

    // 工具调用开始：卡片内嵌到当前 AI 回复气泡上方（toolCards 随消息持久化）
    case "toolStart": {
      // 幂等去重：LLM 流式 chunk 可能重复下发同一 tool_call（相同 callId），
      // 已存在则跳过，避免同 key 卡片重复挂载（React duplicate key 警告）
      const exists = state.messages.some(
        (m) => m.role === "assistant" && m.toolCards?.some((c) => c.callId === action.callId),
      );
      if (exists) return state;

      const card: ToolCardData = {
        callId: action.callId,
        name: action.name,
        args: action.args,
        hint: action.hint,
        status: "pending",
        result: undefined,
      };
      const messages = [...state.messages];
      // 优先挂到正在流式输出的 assistant 消息（本轮的回复气泡）
      const idx = messages.findIndex((m) => m.role === "assistant" && m.streaming);
      if (idx === -1) {
        // 无流式气泡（工具调用先于文本到达）：新建空 assistant 气泡承载卡片
        messages.push({
          id: `id-${state.idSeq}`,
          role: "assistant",
          content: "",
          streaming: true,
          toolCards: [card],
        });
        return {
          ...state,
          idSeq: state.idSeq + 1,
          messages,
          conversations: syncActiveConversation(state, messages, action.now),
        };
      }
      messages[idx] = {
        ...messages[idx],
        toolCards: [...(messages[idx].toolCards ?? []), card],
      };
      return {
        ...state,
        messages,
        conversations: syncActiveConversation(state, messages, action.now),
      };
    }

    // 工具调用结束：更新对应 assistant 消息内卡片的执行结果与状态
    case "toolEnd": {
      const ok = isResultSuccess(action.result);
      let found = false;
      let messages = state.messages.map((m) => {
        if (m.role !== "assistant" || !m.toolCards?.some((c) => c.callId === action.callId)) {
          return m;
        }
        found = true;
        return {
          ...m,
          toolCards: m.toolCards.map((c) =>
            c.callId === action.callId
              ? {
                  ...c,
                  status: ok ? ("success" as const) : ("failed" as const),
                  result: action.result,
                }
              : c,
          ),
        };
      });
      // 审批恢复场景：后端只补发 tool_call_end（无对应 start），补挂到最后一条 assistant 气泡
      if (!found) {
        const card: ToolCardData = {
          callId: action.callId,
          name: action.name,
          args: {},
          hint: "",
          status: ok ? "success" : "failed",
          result: action.result,
        };
        const lastIdx = messages.length - 1;
        const last = messages[lastIdx];
        if (last && last.role === "assistant") {
          messages[lastIdx] = { ...last, toolCards: [...(last.toolCards ?? []), card] };
        } else {
          messages.push({
            id: `id-${state.idSeq}`,
            role: "assistant",
            content: "",
            streaming: false,
            toolCards: [card],
          });
        }
        return {
          ...state,
          idSeq: state.idSeq + 1,
          messages,
          conversations: syncActiveConversation(state, messages, action.now),
          toolLastOk: { ...state.toolLastOk, [action.name]: ok },
        };
      }
      // 按工具名记录最近一次结果：同工具重试成功会覆盖之前的失败标记
      return {
        ...state,
        messages,
        conversations: syncActiveConversation(state, messages, action.now),
        toolLastOk: { ...state.toolLastOk, [action.name]: ok },
      };
    }

    // 更新任务执行状态条阶段
    case "taskStatus":
      return { ...state, taskStatus: action.status };

    // 内存中保存日志行
    case "appendLog":
      return {
        ...state,
        idSeq: state.idSeq + 1,
        logLines: [...state.logLines, { id: `id-${state.idSeq}`, tool: action.tool, text: action.text }],
      };

    // 保存系统提示
    case "appendSystemNote":
      return {
        ...state,
        idSeq: state.idSeq + 1,
        systemNotes: [...state.systemNotes, { id: `id-${state.idSeq}`, text: action.text }],
      };

    // 清空日志
    case "clearLogs":
      return { ...state, logLines: [], systemNotes: [] };

    // 弹出审批请求窗口
    case "approvalRequired":
      return {
        ...state,
        approval: action.data,
        approvalWaiting: false,
        agentState: "awaiting_approval",
      };

    // 标记审批正在向后端提交中
    case "approvalSubmitting":
      return { ...state, approvalWaiting: true };

    // 审批已处理，关闭弹窗
    case "approvalResolved":
      return { ...state, approval: null, approvalWaiting: false };

    // 记录后端分配的会话线程 ID（同步到当前会话便于持久化）
    case "setThreadId":
      return {
        ...state,
        threadId: action.threadId,
        conversations: state.conversations.map((c) =>
          c.id === state.activeConvId
            ? { ...c, threadId: action.threadId, updatedAt: action.now }
            : c,
        ),
      };

    // 清除当前会话 threadId（后端记忆丢失，降级为新对话）
    case "clearThreadId":
      return {
        ...state,
        threadId: null,
        conversations: state.conversations.map((c) =>
          c.id === state.activeConvId ? { ...c, threadId: null, updatedAt: action.now } : c,
        ),
      };

    // 变更 Agent 状态
    case "agentState":
      return { ...state, agentState: action.status };

    // 用户在审批窗点击拒绝
    case "rejectedLocally":
      return { ...state, taskStatus: "REJECTED" };

    // SSE 流式响应正常结束
    case "streamComplete": {
      // 只要某个工具"最近一次"执行失败，终态就是 FAILED
      const hasFailedTool = Object.values(state.toolLastOk).some((ok) => !ok);
      const messages = closeStreamingBubbles(state.messages);
      return {
        ...state,
        messages,
        conversations: syncActiveConversation(state, messages, action.now),
        taskStatus:
          state.taskStatus === "REJECTED"
            ? "REJECTED"
            : hasFailedTool
              ? "FAILED"
              : "SUCCESS",
        agentState: "completed",
        sending: false,
        errorMessage: null,
      };
    }

    // 流程异常中断或失败
    case "runFailed": {
      const messages = closeStreamingBubbles(state.messages);
      return {
        ...state,
        messages,
        conversations: syncActiveConversation(state, messages, action.now),
        taskStatus: "FAILED",
        agentState: "error",
        sending: false,
        approval: null,
        approvalWaiting: false,
        errorMessage: action.message,
      };
    }

    // 用户主动中止 SSE 请求：释放界面锁定，状态回归 idle（非 error）
    case "userAbort": {
      const messages = closeStreamingBubbles(state.messages);
      return {
        ...state,
        messages,
        conversations: syncActiveConversation(state, messages, action.now),
        sending: false,
        agentState: "idle",
        approval: null,
        approvalWaiting: false,
        errorMessage: null,
      };
    }
  }
}

// 状态文本映射
const AGENT_STATE_LABELS: Record<AgentState, string> = {
  idle: "空闲",
  running: "运行中",
  awaiting_approval: "等待审批",
  completed: "已完成",
  error: "出错",
};

// 状态对应 CSS 样式类名
const AGENT_STATE_CLASS: Record<AgentState, string> = {
  idle: styles.stateIdle,
  running: styles.stateRunning,
  awaiting_approval: styles.stateAwaiting,
  completed: styles.stateCompleted,
  error: styles.stateError,
};

export default function Home() {
  // 初始状态完全确定（SSR 与客户端首次渲染一致，避免水合不一致）；
  // localStorage 的恢复放在挂载后的 effect 里 dispatch 完成
  const [state, dispatch] = useReducer(reducer, initialState);
  // 当前请求的 abort 句柄（页面卸载时中止，避免卸载后 setState）
  const handleRef = useRef<PostChatHandle | null>(null);

  // stateRef：保持最新的 state 引用，供 useCallback 空依赖闭包内读取最新 state
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    // 页面卸载时中止活跃的网络请求
    return () => handleRef.current?.abort();
  }, []);

  // 客户端挂载后从 localStorage 恢复会话列表。
  // 注意：必须在写盘 effect 之前声明（水合完成后触发，属客户端更新，无 mismatch；
  // dispatch 由 StrictMode 双执行时靠 reducer 幂等防护）
  useEffect(() => {
    dispatch({
      type: "restoreConversations",
      conversations: loadConversations(),
      deletedThreadIds: loadDeletedThreadIds(),
      blankId: genConversationId(),
      now: Date.now(),
    });
  }, []);

  // 启动时从后端拉取权威历史，合并进本地会话列表（方案 B）。
  // ref 防 StrictMode 双执行；单个线程拉取失败不阻塞整体
  const syncRef = useRef(false);
  const syncWithBackend = useCallback(async () => {
    let threads;
    try {
      threads = await fetchBackendThreads();
    } catch (err) {
      log.error("sync backend threads failed", {
        err: err instanceof Error ? err.message : String(err),
      });
      dispatch({
        type: "appendSystemNote",
        text: `[系统] 后端记忆同步失败: ${err instanceof Error ? err.message : String(err)}`,
      });
      return;
    }
    if (threads.length === 0) return;

    const merged: Conversation[] = [];
    await Promise.all(
      threads.map(async (t) => {
        try {
          const msgs = await fetchBackendThreadMessages(t.thread_id);
          merged.push({
            id: genConversationId(),
            title: t.title || "新对话",
            threadId: t.thread_id,
            messages: toFrontendMessages(msgs, t.thread_id),
            updatedAt: toTimestamp(t.updated_at),
          });
        } catch (err) {
          // 单线程拉取失败：跳过该线程，不影响其他线程与整体流程
          log.warn("fetch backend thread skipped", {
            threadId: t.thread_id,
            err: err instanceof Error ? err.message : String(err),
          });
        }
      }),
    );
    if (merged.length === 0) return;
    log.info("backend threads merged", { count: merged.length });
    dispatch({ type: "mergeBackendThreads", conversations: merged });
  }, []);

  useEffect(() => {
    if (syncRef.current) return;
    syncRef.current = true;
    void syncWithBackend();
  }, [syncWithBackend]);

  // 会话列表变化 → 写回 localStorage（外部系统同步，失败不中断主流程，仅记录日志）
  useEffect(() => {
    // 恢复前 conversations 为空（初始态），跳过写盘，避免用空列表清空已有数据
    if (state.conversations.length === 0) return;
    try {
      window.localStorage.setItem(CONVERSATIONS_STORAGE_KEY, JSON.stringify(state.conversations));
    } catch (err) {
      log.error("save conversations failed", {
        err: err instanceof Error ? err.message : String(err),
      });
    }
  }, [state.conversations]);

  // deletedThreadIds 变化 → 写回 localStorage（防止刷新后后端合并拉回已删会话）
  useEffect(() => {
    try {
      window.localStorage.setItem(DELETED_THREADS_STORAGE_KEY, JSON.stringify([...state.deletedThreadIds]));
    } catch (err) {
      log.error("save deletedThreadIds failed", {
        err: err instanceof Error ? err.message : String(err),
      });
    }
  }, [state.deletedThreadIds]);

  // 点击侧边栏历史会话：载入该会话的 messages/threadId，恢复多轮上下文
  const selectConversation = useCallback((id: string) => {
    log.debug("conversation selected", { id });
    dispatch({ type: "loadConversation", id });
  }, []);

  // 新建对话：创建一条空白会话并置为当前活跃项
  const newConversation = useCallback(() => {
    log.debug("conversation created");
    dispatch({ type: "createConversation", id: genConversationId(), now: Date.now() });
  }, []);

  // 删除会话：先同步删除后端 SQLite 检查点数据，再更新前端状态
  // 后端删除失败不阻断前端删除（前端 localStorage 仍会移除，deletedThreadIds 仍会记录）
  const deleteConversation = useCallback((id: string) => {
    // 从当前会话列表中查找被删会话的 threadId，用于同步删除后端数据
    const conv = stateRef.current.conversations.find((c) => c.id === id);
    const threadId = conv?.threadId;
    log.debug("conversation deleted", { id, threadId });

    // 异步删除后端检查点数据（不 await，不阻塞 UI 响应）
    if (threadId) {
      void deleteBackendThread(threadId);
    }

    dispatch({ type: "deleteConversation", id, newId: genConversationId(), now: Date.now() });
  }, []);

  // SSE 事件分发与处理
  const handleEvent = useCallback((event: SseEvent) => {
    switch (event.type) {
      case "agent_state": {
        if (event.thread_id) {
          dispatch({ type: "setThreadId", threadId: event.thread_id, now: Date.now() });
        }
        const status: AgentState =
          event.status === "awaiting_approval"
            ? "awaiting_approval"
            : event.status === "completed"
              ? "completed"
              : "running";
        dispatch({ type: "agentState", status });
        break;
      }

      case "message_delta": {
        if (typeof event.text === "string" && event.text.length > 0) {
          dispatch({ type: "assistantDelta", text: event.text, now: Date.now() });
        }
        break;
      }

      case "tool_call_start": {
        dispatch({
          type: "toolStart",
          name: event.name,
          args: event.args,
          callId: event.call_id,
          hint: event.hint,
          now: Date.now(),
        });
        break;
      }

      case "tool_call_end": {
        dispatch({
          type: "toolEnd",
          name: event.name,
          callId: event.call_id,
          result: event.result,
          now: Date.now(),
        });
        break;
      }

      case "task_status": {
        if (typeof event.status === "string" && event.status.length > 0) {
          dispatch({ type: "taskStatus", status: event.status });
        }
        break;
      }

      case "log": {
        if (typeof event.text === "string" && event.text.length > 0) {
          dispatch({ type: "appendLog", tool: event.tool, text: event.text });
        }
        break;
      }

      // build_log（实时构建日志）：前端不展示，事件被解析后直接忽略

      case "approval_required": {
        if (!Array.isArray(event.action_requests)) {
          log.error("approval_required missing action_requests");
          dispatch({
            type: "appendSystemNote",
            text: "[系统] 收到无效审批事件（缺少 action_requests 数组）",
          });
          break;
        }
        log.debug("approval dialog shown", { actions: event.action_requests.length });
        dispatch({
          type: "approvalRequired",
          data: {
            threadId: event.thread_id,
            actionRequests: event.action_requests,
            reviewConfigs: Array.isArray(event.review_configs) ? event.review_configs : [],
          },
        });
        break;
      }

      case "stream_complete": {
        log.info("stream complete");
        dispatch({ type: "assistantFinal", text: event.final_result, now: Date.now() });
        dispatch({ type: "streamComplete", now: Date.now() });
        break;
      }

      case "error": {
        log.error("backend error event", { message: event.message });
        dispatch({
          type: "appendSystemNote",
          text: `[系统] 后端错误: ${event.message}`,
        });
        dispatch({ type: "runFailed", message: event.message, now: Date.now() });
        break;
      }
    }
  }, []);

  const onFrontendError = useCallback((err: Error) => {
    log.error("request failed", { err: err.message });
    dispatch({
      type: "appendSystemNote",
      text: `[系统] 请求失败: ${err.message}`,
    });
    dispatch({ type: "runFailed", message: err.message, now: Date.now() });
  }, []);

  const onStreamClose = useCallback(() => {
    log.warn("stream closed without terminal event");
    dispatch({
      type: "appendSystemNote",
      text: "[系统] 连接中断（未收到 stream_complete/error 事件）",
    });
    dispatch({ type: "runFailed", message: "连接中断", now: Date.now() });
  }, []);

  const onParseSkip = useCallback((reason: string) => {
    log.error("sse parse skip", { reason });
    dispatch({ type: "appendSystemNote", text: `[系统] SSE 解析异常: ${reason}` });
  }, []);

  // 发送消息处理方法（首条消息自动标题在 reducer userMessage 内处理）
  const sendMessage = useCallback(
    async (text: string) => {
      if (state.sending || state.agentState === "running" || state.agentState === "awaiting_approval") {
        return;
      }
      dispatch({ type: "userMessage", text, now: Date.now() });
      dispatch({ type: "sendStart" });

      // 方案 C：续聊前探活后端线程。后端重启会清空（旧 InMemorySaver）或
      // 丢失（文件被删）记忆 → 提示并清空 threadId，本次作为新对话开始
      let effectiveThreadId = state.threadId;
      if (effectiveThreadId) {
        try {
          await fetchBackendThreadMessages(effectiveThreadId);
        } catch (err) {
          if (err instanceof ThreadNotFoundError) {
            log.warn("thread memory lost on backend, restart as new thread", {
              threadId: effectiveThreadId,
            });
            dispatch({
              type: "appendSystemNote",
              text: "[系统] 后端记忆已丢失（服务可能重启），本次将作为新对话开始",
            });
            dispatch({ type: "clearThreadId", now: Date.now() });
            effectiveThreadId = null;
          } else {
            // 探活本身失败（网络/后端不可达）：不阻塞发送，由请求自身报错
            log.warn("thread probe failed, send anyway", {
              threadId: effectiveThreadId,
              err: err instanceof Error ? err.message : String(err),
            });
          }
        }
      }

      handleRef.current = postChat(
        {
          message: text,
          ...(effectiveThreadId ? { threadId: effectiveThreadId } : {}),
        },
        {
          onEvent: handleEvent,
          onStreamError: onFrontendError,
          onStreamClose,
          onParseSkip,
        },
      );
    },
    [
      state.sending,
      state.agentState,
      state.threadId,
      handleEvent,
      onFrontendError,
      onStreamClose,
      onParseSkip,
    ],
  );

  // 审批决策处理方法
  const decideApproval = useCallback(
    (decisions: Array<{ type: ApprovalDecisionType }>) => {
      if (!state.threadId) {
        log.error("approval decided without threadId");
        dispatch({
          type: "appendSystemNote",
          text: "[系统] 审批提交失败：缺少 threadId",
        });
        return;
      }
      log.debug("approval decided", { type: decisions[0]?.type });
      if (decisions[0]?.type === "reject") {
        dispatch({ type: "rejectedLocally" });
      }
      dispatch({ type: "approvalSubmitting" });
      dispatch({ type: "sendStart" });

      // 恢复流第一个事件到达后关闭弹窗（"收到后续事件继续渲染"）
      let resumed = false;
      handleRef.current = postChat(
        { threadId: state.threadId, message: "", decisions },
        {
          onEvent: (event) => {
            if (!resumed) {
              resumed = true;
              dispatch({ type: "approvalResolved" });
            }
            handleEvent(event);
          },
          onStreamError: onFrontendError,
          onStreamClose,
          onParseSkip,
        },
      );
    },
    [state.threadId, handleEvent, onFrontendError, onStreamClose, onParseSkip],
  );

  const composerDisabled =
    state.sending || state.agentState === "running" || state.agentState === "awaiting_approval";
  // 请求进行中禁止切换/新建/删除会话（避免流事件写入错误的会话）
  const sidebarDisabled = state.sending || state.agentState === "running";

  // Agent 流式响应中：显示「停止响应」按钮
  const isStreaming = state.sending && state.agentState === "running";

  // 新建对话欢迎态的快捷提示词：每个会话固定一组（切换会话重新抽取）
  const suggestions = useMemo(
    () => pickSuggestions(3, state.activeConvId || "welcome"),
    [state.activeConvId],
  );

  // 用户主动中止请求：截断 SSE + 释放界面锁定
  const handleAbort = useCallback(() => {
    handleRef.current?.abort();
    dispatch({ type: "userAbort", now: Date.now() });
    log.info("user aborted the response");
  }, []);

  return (
    <div className={styles.page}>
      {/* 左侧：历史会话侧边栏 */}
      <Sidebar
        conversations={state.conversations}
        activeId={state.activeConvId}
        disabled={sidebarDisabled}
        onSelect={selectConversation}
        onNew={newConversation}
        onDelete={deleteConversation}
      />

      {/* 右侧：主工作区 */}
      <div className={styles.main}>
{/* 顶部标题与操作栏 */}
        <header className={styles.header}>
          <div className={styles.headerLeft}>
            <h1 className={styles.logo}>AI_DevOps</h1>
            {/* 新建会话重置按钮（与侧边栏「新建对话」同一逻辑） */}
           
          </div>
          <span className={`${styles.stateBadge} ${AGENT_STATE_CLASS[state.agentState]}`}>
            {AGENT_STATE_LABELS[state.agentState]}
          </span>
        </header>

        {/* 任务状态指示条 */}
        <TaskStatusBar status={state.taskStatus} />

        {/* 核心展示主体区域 */}
        <main className={styles.body}>
          {state.messages.length === 0 ? (
            /* 新建对话欢迎态：垂直居中大标题 + 居中输入框 + 随机快捷功能胶囊 */
            <div className={styles.welcome}>
              <h1 className={styles.welcomeTitle}>今天有什么部署任务？</h1>
              <Composer
                centered
                disabled={composerDisabled}
                onSend={sendMessage}
                isStreaming={isStreaming}
                onAbort={handleAbort}
              />
              <div className={styles.chips}>
                {suggestions.map((s, i) => (
                  <button
                    key={s}
                    type="button"
                    className={styles.chip}
                    style={{ animationDelay: `${120 + i * 80}ms` }}
                    onClick={() => sendMessage(s)}
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <>
              {/* 对话消息区域（工具调用卡片内嵌在 AI 气泡上方，随消息持久保留） */}
              <div className={styles.messageArea}>
                <MessageList messages={state.messages} errorMessage={state.errorMessage} />
              </div>
              {/* 底部悬浮输入框 */}
              <Composer
                disabled={composerDisabled}
                onSend={sendMessage}
                isStreaming={isStreaming}
                onAbort={handleAbort}
              />
            </>
          )}
        </main>
      </div>

      {/* 审批弹窗 */}
      {state.approval && (
        <ApprovalDialog
          data={state.approval}
          waiting={state.approvalWaiting}
          onDecide={decideApproval}
        />
      )}
    </div>
  );
}