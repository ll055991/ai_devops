// ---------------------------------------------------------------------------
// 日志区（任务 2.3）
//
// 职责：log 事件追加到滚动容器（docker build 输出），自动滚到底部，带清空按钮；
//       [系统] 灰字提示固定显示在顶部，区分前端还是后端问题。
//
// 说明：
//   - 系统提示（fetch 失败 / SSE 解析异常 / 后端 error 事件）来自 systemNotes，
//     渲染在日志区顶部（不参与自动滚动），灰字标注，来源一目了然。
//   - 工具日志行格式："[工具名] 内容"，方便区分是哪个工具的输出。
// ---------------------------------------------------------------------------

"use client";

import { useEffect, useRef } from "react";
import styles from "./LogPanel.module.css";

// 工具日志行（log 事件追加）
export interface LogLine {
  id: string;
  tool: string;
  text: string;
}

// 前端系统提示（任务 2.3-8：日志区顶部 [系统] ... 灰字）
export interface SystemNote {
  id: string;
  text: string;
}

interface LogPanelProps {
  logLines: LogLine[];
  systemNotes: SystemNote[];
  onClear: () => void;
}

export default function LogPanel({ logLines, systemNotes, onClear }: LogPanelProps) {
  // 滚动容器 ref：log 追加后自动滚到底部
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [logLines]);

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.title}>构建日志</span>
        <button type="button" className={styles.clearBtn} onClick={onClear}>
          清空
        </button>
      </div>

      {systemNotes.length > 0 && (
        <div className={styles.systemNotes}>
          {systemNotes.map((note) => (
            <p key={note.id} className={styles.systemNote}>
              {note.text}
            </p>
          ))}
        </div>
      )}

      <div ref={scrollRef} className={styles.body}>
        {logLines.length === 0 && (
          <p className={styles.empty}>暂无日志输出</p>
        )}
        {logLines.map((line) => (
          <p key={line.id} className={styles.line}>
            {line.tool && <span className={styles.toolTag}>[{line.tool}]</span>}
            {line.text}
          </p>
        ))}
      </div>
    </div>
  );
}