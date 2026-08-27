// ---------------------------------------------------------------------------
// 工具卡片（任务 2.3）
//
// 职责：tool_call_start 插入卡片（图标 + 工具名 + 参数折叠，pending 态），
//       tool_call_end 更新结果（成功绿勾 / 失败红叉）。
//
// 样式思路参考 ai-native/components/assistant-ui/tool-fallback.tsx：
//   - 触发器行：图标 + 工具显示名 + 状态图标 + 折叠箭头
//   - 内容区：参数（输入）可折叠、结果可折叠（JSON 美化）
//   - 运行中展开、完成后自动折叠（参考 ToolFallbackImpl 的 isOpen 逻辑）
//   - 失败结果优先展示 error_type + message（参考 ToolFallbackError）
//
// 不引入图标库：工具名 → 图标用内联 SVG 映射（参考 tool-fallback/constants.ts
// 的 TOOL_ICONS 思路，但用最小 SVG 集避免新增依赖）。
// ---------------------------------------------------------------------------

"use client";

import { useEffect, useRef, useState } from "react";
import styles from "./ToolCard.module.css";

// 工具卡片数据（page.tsx reducer 维护，按 call_id 索引）
export interface ToolCardData {
  callId: string;
  name: string;
  args: Record<string, unknown>;
  hint: string;
  status: "pending" | "success" | "failed";
  result: unknown;
}

interface ToolCardProps {
  card: ToolCardData;
}

// 工具名 → 中文显示名（参考 tool-fallback/constants.ts 的 TOOL_NAMES 思路）
const TOOL_DISPLAY_NAMES: Record<string, string> = {
  git_pull_code: "拉取代码",
  build_docker_image: "构建镜像",
  stop_container: "停止容器",
  remove_container: "删除容器",
  start_container: "启动容器",
  check_service_health: "健康检查",
  list_containers: "查询容器列表",
  list_images: "查询镜像列表",
  check_dockerfile: "检查 Dockerfile",
  list_workspace_files: "列出工作目录文件",
  read_workspace_file: "读取文件",
  read_file: "读取文件",
  write_workspace_file: "写入文件",
  delete_workspace_file: "删除文件",
};

function getToolDisplayName(name: string): string {
  return TOOL_DISPLAY_NAMES[name] ?? name;
}

// 工具名 → 图标（分组映射，未登记工具用默认扳手图标）
function getToolIcon(name: string): React.ReactNode {
  if (name.startsWith("git")) return <GitIcon />;
  if (name.startsWith("build")) return <PackageIcon />;
  if (name.includes("container")) return <ContainerIcon />;
  if (name.startsWith("check_service")) return <PulseIcon />;
  if (name.startsWith("list_")) return <ListIcon />;
  if (name.startsWith("check_")) return <FileIcon />;
  if (name.includes("read_") || name.includes("write_") || name.includes("delete_")) {
    return <FileIcon />;
  }
  return <WrenchIcon />;
}

// ---- 内联 SVG 图标（16px，stroke 风格，参考 lucide 的视觉语言） ----
type IconProps = React.SVGProps<SVGSVGElement>;

function WrenchIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden {...props}>
      <path d="M14.7 6.3a4.5 4.5 0 0 0-6 6L3 18l3 3 5.7-5.7a4.5 4.5 0 0 0 6-6L14 13l-3-3 3.7-3.7z" />
    </svg>
  );
}

function GitIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden {...props}>
      <circle cx={6} cy={6} r={3} />
      <circle cx={6} cy={18} r={3} />
      <circle cx={18} cy={8} r={3} />
      <path d="M6 9v6M18 11c0 3-4 4-6 4" />
    </svg>
  );
}

function PackageIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden {...props}>
      <path d="M21 8 12 3 3 8v8l9 5 9-5V8z" />
      <path d="M3 8l9 5 9-5M12 13v8" />
    </svg>
  );
}

function ContainerIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden {...props}>
      <path d="M3 8l4-3 4 3v11l-4 3-4-3V8z" />
      <path d="M11 8l4-3 4 3v11l-4 3-4-3V8z" />
    </svg>
  );
}

function PulseIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden {...props}>
      <path d="M22 12h-4l-3 8L9 4l-3 8H2" />
    </svg>
  );
}

function ListIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden {...props}>
      <path d="M8 6h13M8 12h13M8 18h13" />
      <path d="M3 6h.01M3 12h.01M3 18h.01" />
    </svg>
  );
}

function FileIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden {...props}>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6z" />
      <path d="M14 2v6h6" />
    </svg>
  );
}

function ChevronIcon({ open }: { open: boolean }) {
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
      className={open ? styles.chevronOpen : styles.chevronClosed}
    >
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}

// 结果展示时剔除的冗余字段：
// - raw：后端 list_workspace_files 返回的原始 ls 输出（tools/__init__.py），
//   仅供 LLM 阅读，前端已有结构化 files 数组，展示会重复且撑大卡片
const RESULT_HIDDEN_FIELDS = new Set(["raw"]);

// 结果格式化：JSON 字符串尝试解析美化，对象直接美化，其余转字符串
// （参考 tool-fallback-parts.tsx 的 ToolFallbackResult 实现思路）
// 对象中的冗余字段（raw）在美化前剔除
function formatResult(result: unknown): string {
  if (typeof result === "string") {
    try {
      const parsed = JSON.parse(result);
      if (typeof parsed === "object" && parsed !== null) {
        return formatResult(parsed);
      }
      return result;
    } catch {
      return result;
    }
  }
  if (typeof result === "object" && result !== null) {
    const obj = result as Record<string, unknown>;
    const filtered = Object.fromEntries(
      Object.entries(obj).filter(([k]) => !RESULT_HIDDEN_FIELDS.has(k)),
    );
    return JSON.stringify(filtered, null, 2);
  }
  return String(result);
}

// 判断工具结果是否成功：结构化 result.success === true（参考 api.py 的 _ok/_err）
// 内置工具（如 deepagents 的 read_file）返回纯文本，非 {success: true}，
// 一律视为成功（错误会以 "Error:" 开头），避免阅读类卡片误标失败。
function isResultSuccess(result: unknown): boolean {
  if (typeof result === "string") {
    return !/^error[:\s]/i.test(result.trim());
  }
  return (
    typeof result === "object" &&
    result !== null &&
    (result as { success?: unknown }).success === true
  );
}

// 判断文本是否过长（>300 字符或 >15 行）：过长时折叠展示
function isLongText(text: string): boolean {
  if (text.length > 300) return true;
  return text.split("\n").length > 15;
}

// 结果展示：超长文本顶部提示摘要 + 240px 可滚动安全窗口，严禁撑高父容器
function ResultContent({ result }: { result: unknown }) {
  const formatted = formatResult(result);
  const long = isLongText(formatted);
  return (
    <>
      {long && (
        <p className={styles.longHint}>
          内容过长，已折叠展示（共 {formatted.split("\n").length} 行 / {formatted.length} 字符）
        </p>
      )}
      <pre className={styles.pre}>{formatted}</pre>
    </>
  );
}

// 工具是否为纯阅读类（read_file / read_workspace_file）：默认折叠，不自动弹开刷屏
function isReadOnlyTool(name: string): boolean {
  return name === "read_file" || name === "read_workspace_file";
}

// 读取文件的路径（兼容 deepagents 内置 read_file 的 path 与自定义 read_workspace_file 的 file_path）
function getReadPath(card: ToolCardData): string {
  const result = card.result;
  const target =
    typeof result === "object" && result !== null
      ? (result as Record<string, unknown>).target
      : undefined;
  if (typeof target === "string" && target) return target;
  const args = card.args as Record<string, unknown>;
  for (const key of ["path", "file_path"]) {
    const v = args[key];
    if (typeof v === "string" && v) return v;
  }
  return "";
}

// 是否正在阅读技能手册（/skills/ 路径）
function isSkillsRead(card: ToolCardData): boolean {
  return getReadPath(card).includes("/skills/");
}

export default function ToolCard({ card }: ToolCardProps) {
  const isRunning = card.status === "pending";
  // 参考 tool-fallback-impl.tsx 的 isOpen 逻辑：运行中展开、结束后自动折叠；
  // 阅读类工具（read_workspace_file）默认保持折叠，避免读规则时卡片自动弹开刷屏
  const readOnly = isReadOnlyTool(card.name);
  const [open, setOpen] = useState(isRunning && !readOnly);
  const wasRunningRef = useRef(isRunning);

  useEffect(() => {
    if (wasRunningRef.current && !isRunning) {
      setOpen(false);
      return;
    }
    if (!wasRunningRef.current && isRunning && !readOnly) {
      setOpen(true);
    }
    wasRunningRef.current = isRunning;
  }, [isRunning, readOnly]);

  const failed = card.status === "failed";
  // 失败时提取 error_type + message 优先展示（参考规划方案 3.5）
  let errorType: string | null = null;
  let errorMessage: string | null = null;
  if (failed && typeof card.result === "object" && card.result !== null) {
    const r = card.result as Record<string, unknown>;
    errorType = typeof r.error_type === "string" ? r.error_type : null;
    errorMessage = typeof r.message === "string" ? r.message : null;
  }

  // 阅读类工具（read_file / read_workspace_file）：只展示正在阅读的文件路径，不展示文件内容
  const skillsRead = isSkillsRead(card);
  const readFileShown = getReadPath(card) || null;

  return (
    <div className={`${styles.card} ${failed ? styles.cardFailed : ""}`}>
      <button
        type="button"
        className={styles.trigger}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span className={styles.icon}>{getToolIcon(card.name)}</span>
        <span className={styles.name}>{getToolDisplayName(card.name)}</span>

        {card.hint && isRunning && <span className={styles.hint}>{card.hint}</span>}

        {isRunning && <span className={styles.pending} title="运行中">运行中</span>}
        {!isRunning && !failed && <span className={styles.ok} title="成功">✓</span>}
        {failed && <span className={styles.err} title="失败">✕</span>}

        <ChevronIcon open={open} />
      </button>

      {open && (
        <div className={styles.body}>
          {Object.keys(card.args).length > 0 && (
            <div className={styles.section}>
              <span className={styles.sectionLabel}>参数</span>
              <pre className={styles.pre}>{JSON.stringify(card.args, null, 2)}</pre>
            </div>
          )}

          {!isRunning && (
            <div className={styles.section}>
              <span className={styles.sectionLabel}>结果</span>
              {failed && (errorType || errorMessage) ? (
                <p className={styles.errorLine}>
                  {errorType ? `${errorType}: ` : ""}
                  {errorMessage ?? formatResult(card.result)}
                </p>
              ) : readOnly ? (
                // 阅读类工具：只显示正在阅读的文件路径，不展示文件内容；
                // 读取 /skills/ 技能手册时强化提示
                <p className={styles.okLine}>
                  {skillsRead
                    ? "已查阅技能部署手册"
                    : readFileShown
                      ? `已读取：${readFileShown}`
                      : "已读取文件"}
                </p>
              ) : card.result === undefined || card.result === null || card.result === "" ? (
                // 成功后端未带 result 字段：显示「执行成功」而非空内容/undefined
                <p className={styles.okLine}>执行成功</p>
              ) : (
                <ResultContent result={card.result} />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// 导出供页面判断工具级失败（stream_complete 推断 FAILED 终态时用）
export { isResultSuccess };