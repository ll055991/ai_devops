// ---------------------------------------------------------------------------
// 发送框（自适应高度多行文本域 + 停止响应）
//
// 职责：
//  - textarea 自适应高度（上限 160px），Shift+Enter 换行，Enter 发送
//  - disabled 由父组件控制：sending / running / awaiting_approval 时禁用
//    （审批弹窗期间也禁用，避免绕过审批发新消息）
//  - 空输入或全空白时不发送
//  - Agent 流式响应中显示「停止响应」按钮，点击触发 abort() 截断 SSE
// ---------------------------------------------------------------------------

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import styles from "./Composer.module.css";

/** textarea 自适应高度上限（px） */
const MAX_HEIGHT = 160;

interface ComposerProps {
  disabled: boolean;
  onSend: (text: string) => void;
  /** 是否正在流式响应中（为 true 时显示「停止响应」按钮） */
  isStreaming: boolean;
  /** 用户主动中止 SSE 请求 */
  onAbort: () => void;
  /** 居中模式（新建对话欢迎态）：输入框作为居中布局的一部分，非底部悬浮 */
  centered?: boolean;
}

export default function Composer({
  disabled,
  onSend,
  isStreaming,
  onAbort,
  centered = false,
}: ComposerProps) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // 根据 scrollHeight 自适应 textarea 高度，上限 MAX_HEIGHT
  const adjustHeight = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    // 先重置为 auto 以获取真实 scrollHeight
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, []);

  // 内容变化时同步调整高度
  useEffect(() => {
    adjustHeight();
  }, [text, adjustHeight]);

  const handleSend = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText("");
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Shift+Enter：换行（textarea 默认行为，不拦截）
    // 单独 Enter：发送（中文输入法组词中不触发，避免误发）
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    }
  };

  // 发送后重置 textarea 高度
  const handleSendClick = () => {
    handleSend();
    // 发送后文本已清空，需在下一帧重置高度（setText("") 尚未渲染）
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) {
        el.style.height = "auto";
        el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
      }
    });
  };

  return (
    <div className={`${styles.composer} ${centered ? styles.composerCentered : ""}`}>
      <textarea
        ref={textareaRef}
        className={styles.textarea}
        placeholder={disabled ? "等待 Agent 响应…" : "输入部署任务，Enter 发送，Shift+Enter 换行"}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={disabled}
        rows={1}
      />
      {isStreaming ? (
        <button
          type="button"
          className={styles.stopBtn}
          onClick={onAbort}
          title="停止响应"
          aria-label="停止响应"
        >
          <svg viewBox="0 0 24 24" width={16} height={16} fill="currentColor" aria-hidden>
            <rect x={6} y={6} width={12} height={12} rx={2} />
          </svg>
        </button>
      ) : (
        <button
          type="button"
          className={styles.sendBtn}
          onClick={handleSendClick}
          disabled={disabled || text.trim() === ""}
          title="发送"
          aria-label="发送"
        >
          <svg
            viewBox="0 0 24 24"
            width={18}
            height={18}
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden
          >
            <path d="M12 19V5" />
            <path d="m5 12 7-7 7 7" />
          </svg>
        </button>
      )}
    </div>
  );
}