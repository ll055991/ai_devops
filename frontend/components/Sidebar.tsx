// ---------------------------------------------------------------------------
// 历史对话侧边栏
//
// 职责：顶部「+ 新建对话」按钮；垂直列出所有历史会话（标题 + 更新时间），
//       高亮当前激活项；悬浮显示垃圾桶图标支持单项删除。
//
// 数据由 page.tsx 统一管理（localStorage 持久化），本组件只做展示与事件回调。
// ---------------------------------------------------------------------------

"use client";

import type { Message } from "./MessageList";
import styles from "./Sidebar.module.css";

// 会话数据结构（与 localStorage 持久化格式一致）
export interface Conversation {
  id: string;
  title: string;
  threadId: string | null;
  messages: Message[];
  updatedAt: number;
}

// 更新时间格式化：MM-DD HH:mm
function formatTime(ts: number): string {
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// 悬浮删除按钮的垃圾桶图标（内联 SVG，避免新增依赖）
function TrashIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width={14}
      height={14}
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V6" />
      <path d="M10 11v6M14 11v6" />
    </svg>
  );
}

interface SidebarProps {
  conversations: Conversation[];
  // 当前激活会话 id（高亮）
  activeId: string;
  // true：请求进行中，禁止切换/新建/删除（避免流事件写入错误的会话）
  disabled?: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
}

export default function Sidebar({
  conversations,
  activeId,
  disabled = false,
  onSelect,
  onNew,
  onDelete,
}: SidebarProps) {
  return (
    <aside className={styles.sidebar}>
      <div className={styles.header}>
        <span className={styles.title}>历史对话</span>
        <button
          type="button"
          className={styles.newBtn}
          onClick={onNew}
          disabled={disabled}
          title={disabled ? "请求进行中，暂不可新建" : "新建对话"}
        >
          + 新建对话
        </button>
      </div>

      <ul className={styles.list}>
        {conversations.length === 0 && <li className={styles.empty}>暂无会话</li>}

        {conversations.map((c) => (
          <li key={c.id}>
            <button
              type="button"
              className={`${styles.item} ${c.id === activeId ? styles.itemActive : ""}`}
              onClick={() => onSelect(c.id)}
              disabled={disabled}
            >
              <span className={styles.itemMain}>
                <span className={styles.itemTitle}>{c.title || "新对话"}</span>
                <span className={styles.itemTime}>{formatTime(c.updatedAt)}</span>
              </span>
              {/* 悬浮显示的删除按钮（span 而非嵌套 button，避免非法 HTML 嵌套） */}
              <span
                role="button"
                aria-label="删除会话"
                title="删除会话"
                className={styles.delBtn}
                onClick={(e) => {
                  e.stopPropagation();
                  if (!disabled && window.confirm("确定删除该对话？删除后不可恢复。")) onDelete(c.id);
                }}
              >
                <TrashIcon />
              </span>
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
}