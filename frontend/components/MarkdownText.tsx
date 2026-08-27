// ---------------------------------------------------------------------------
// Markdown 文本渲染（任务 2.3 优化）
//
// 职责：Agent 回复文本按 GFM 渲染（支持表格），并在渲染前剥离混入的
//       工具结果原始 JSON（lib/text-cleanup.ts）。
//
// 安全：react-markdown 默认不渲染原始 HTML（需显式 rehype-raw 才开启），
//       链接默认过滤 javascript: 协议，无 innerHTML 注入面。
// ---------------------------------------------------------------------------

"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { stripToolResultJson } from "@/lib/text-cleanup";
import styles from "./MarkdownText.module.css";

interface MarkdownTextProps {
  text: string;
}

export default function MarkdownText({ text }: MarkdownTextProps) {
  // 渲染前先剥离工具结果 JSON（提示词已约束，此处前端兜底）
  const clean = stripToolResultJson(text);
  return (
    <div className={styles.markdown}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{clean}</ReactMarkdown>
    </div>
  );
}