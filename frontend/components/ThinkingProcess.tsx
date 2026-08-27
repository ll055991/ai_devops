// ---------------------------------------------------------------------------
// 深度思考卡片组件（仿豆包风格）
//
// 职责：将模型的思考过程以可折叠卡片形式展示，与最终回答解耦。
//   - 思考进行中：显示旋转 Loading 图标 + "正在思考中..." + 展开实时内容
//   - 思考完成：显示勾选图标 + "已完成深度思考" + 可折叠/展开
// ---------------------------------------------------------------------------

"use client";

import { useState } from "react";
import styles from "./ThinkingProcess.module.css";

interface ThinkingProcessProps {
  /** 思考内容文本 */
  thought: string;
  /** 是否正在思考中（true=进行中，false=已完成） */
  isThinking: boolean;
}

export default function ThinkingProcess({ thought, isThinking }: ThinkingProcessProps) {
  // 思考进行中默认展开，完成后默认收起
  const [expanded, setExpanded] = useState(isThinking);

  // 完成态切换时，自动收起（只触发一次：isThinking 从 true 变 false 时）
  // 用 key 重新挂载来避免复杂的状态同步
  return (
    <div
      className={`${styles.card} ${isThinking ? styles.thinking : styles.done}`}
      key={isThinking ? "thinking" : "done"}
    >
      {/* 标题栏：图标 + 文字 + 折叠箭头 */}
      <button
        type="button"
        className={styles.header}
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        <span className={styles.icon}>{isThinking ? <SpinnerIcon /> : <CheckIcon />}</span>
        <span className={styles.title}>
          {isThinking ? "正在思考中..." : "已完成深度思考"}
        </span>
        <span className={`${styles.chevron} ${expanded ? styles.chevronOpen : ""}`}>
          <ChevronIcon />
        </span>
      </button>

      {/* 思考内容区（折叠/展开） */}
      {expanded && (
        <div className={styles.body}>
          <pre className={styles.text}>{thought}</pre>
        </div>
      )}
    </div>
  );
}

// ---- 内联 SVG 图标 ----

/** 旋转 Loading 图标 */
function SpinnerIcon() {
  return (
    <svg viewBox="0 0 24 24" width={16} height={16} fill="none" className={styles.spin}>
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="2.5" strokeDasharray="31.4 31.4" strokeLinecap="round" />
    </svg>
  );
}

/** 勾选图标 */
function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 12l2 2 4-4" />
      <circle cx="12" cy="12" r="10" />
    </svg>
  );
}

/** 折叠箭头 */
function ChevronIcon() {
  return (
    <svg viewBox="0 0 24 24" width={14} height={14} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 9l6 6 6-6" />
    </svg>
  );
}
