// ---------------------------------------------------------------------------
// 消息区（任务 2.3）
//
// 职责：渲染用户消息气泡 + Agent 流式文本气泡，工具调用卡片内嵌在
//       AI 气泡上方（支持折叠展开，随消息持久保留在多轮历史中）。
//  - 用户消息：右对齐浅灰气泡（#f4f4f4，圆角 20px）
//  - Agent 消息：左对齐纯文本流式排版（无边框），左侧轻量圆形 ChatGPT 绿图标
//  - streaming 为 true 时显示打字光标，提示还在流式输出
//  - Agent 消息中若含思考标签(think)，拆分为思考卡片 + 最终回答
//  - errorMessage 非空时在末尾追加红色错误横幅（stream_complete/error 处理）
// ---------------------------------------------------------------------------

"use client";

import MarkdownText from "./MarkdownText";
import ThinkingProcess from "./ThinkingProcess";
import ToolCard, { type ToolCardData } from "./ToolCard";
import { parseThinkTags } from "@/lib/think-parser";
import styles from "./MessageList.module.css";

// 单条消息（本地 state，由 page.tsx 的 reducer 维护）
export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  // 是否仍在接收 message_delta 增量（true 时渲染打字光标）
  streaming: boolean;
  // 该轮 AI 回复上方内嵌的工具调用卡片（随会话持久化，多轮历史保留）
  toolCards?: ToolCardData[];
}

interface MessageListProps {
  messages: Message[];
  // 流异常（error 事件 / fetch 失败）时展示的红色错误横幅
  errorMessage: string | null;
}

// ChatGPT 风格助手头像：轻量圆形绿底图标（四角星点缀）
function AssistantIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width={16}
      height={16}
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z" />
      <path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15z" />
    </svg>
  );
}

export default function MessageList({ messages, errorMessage }: MessageListProps) {
  return (
    <div className={styles.container}>
      {messages.length === 0 && !errorMessage && (
        <p className={styles.empty}>输入部署任务开始，例如：部署仓库 … 的 ctc_jt_1.1.1 分支</p>
      )}

      {messages.map((m) => {
        // Agent 消息解析思考标签：拆分 thought 与 finalContent
        const parsed = m.role === "assistant" ? parseThinkTags(m.content) : null;

        return (
          <div
            key={m.id}
            className={m.role === "user" ? styles.userRow : styles.assistantRow}
          >
            {/* 工具调用卡片：内嵌在 AI 气泡上方 */}
            {m.role === "assistant" && m.toolCards && m.toolCards.length > 0 && (
              <div className={styles.toolCards}>
                {m.toolCards.map((card) => (
                  <ToolCard key={card.callId} card={card} />
                ))}
              </div>
            )}

            {m.role === "assistant" ? (
              <div className={styles.assistantLine}>
                <span className={styles.assistantIcon}>
                  <AssistantIcon />
                </span>
                <div className={styles.assistantBubble}>
                  {/* 思考卡片：仅当存在 thought 时渲染 */}
                  {parsed?.thought && (
                    <ThinkingProcess thought={parsed.thought} isThinking={parsed.isThinking} />
                  )}
                  {/* Agent 文本走 Markdown（GFM 表格）+ JSON 剥离 */}
                  {parsed?.finalContent ?? m.content ? (
                    <MarkdownText text={parsed?.finalContent ?? m.content} />
                  ) : null}
                  {m.streaming && <span className={styles.cursor}>▍</span>}
                </div>
              </div>
            ) : (
              <div className={styles.userBubble}>{m.content}</div>
            )}
          </div>
        );
      })}

      {errorMessage && (
        <div className={styles.errorRow}>
          <div className={styles.errorBubble}>{errorMessage}</div>
        </div>
      )}
    </div>
  );
}
