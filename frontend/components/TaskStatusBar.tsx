// ---------------------------------------------------------------------------
// 状态条（任务 2.3）
//
// 职责：task_status 显示当前阶段，覆盖后端 TOOL_STATUS_MAP 全部 9 个阶段
//       （决策 3：状态条覆盖全部 9 个 task_status，巡检工具同样推动状态前进），
//       加上 INIT 起点与 SUCCESS / FAILED / REJECTED 终态。
//
// 终态推断（本任务要求 INIT→...→SUCCESS/FAILED/REJECTED）：
//   - SUCCESS：收到 stream_complete 且本次运行无工具失败
//   - FAILED：收到 error 事件，或 stream_complete 时存在工具失败
//   - REJECTED：用户在审批弹窗点击「拒绝」（后端不发 REJECTED 事件，
//     由前端本地置位，见规划方案备选方案 B1）
// ---------------------------------------------------------------------------

"use client";

import styles from "./TaskStatusBar.module.css";

// 阶段顺序（对齐 backend api.py 的 TOOL_STATUS_MAP）
const STAGES = [
  "GIT_PULL",
  "BUILD_IMAGE",
  "STOP_CONTAINER",
  "REMOVE_CONTAINER",
  "START_CONTAINER",
  "HEALTH_CHECK",
  "LIST_CONTAINERS",
  "LIST_IMAGES",
  "CHECK_DOCKERFILE",
];

// 阶段中文短标签（横向排布空间有限，用短文案）
const STAGE_LABELS: Record<string, string> = {
  GIT_PULL: "拉代码",
  BUILD_IMAGE: "构建镜像",
  STOP_CONTAINER: "停止容器",
  REMOVE_CONTAINER: "删除容器",
  START_CONTAINER: "启动容器",
  HEALTH_CHECK: "健康检查",
  LIST_CONTAINERS: "容器列表",
  LIST_IMAGES: "镜像列表",
  CHECK_DOCKERFILE: "Dockerfile",
};

// 终态文案与颜色语义
const TERMINAL_LABELS: Record<string, string> = {
  SUCCESS: "部署成功",
  FAILED: "部署失败",
  REJECTED: "已拒绝",
};

interface TaskStatusBarProps {
  // 当前阶段：INIT / 9 阶段之一 / SUCCESS / FAILED / REJECTED
  status: string;
}

export default function TaskStatusBar({ status }: TaskStatusBarProps) {
  const activeIndex = STAGES.indexOf(status);
  const isTerminal = status === "SUCCESS" || status === "FAILED" || status === "REJECTED";

  return (
    <div className={styles.bar}>
      <span className={styles.title}>任务状态</span>

      <div className={styles.stages}>
        {/* INIT 起点：初始或尚未进入任何阶段时高亮 */}
        <span
          className={`${styles.chip} ${activeIndex === -1 && !isTerminal ? styles.chipActive : styles.chipDone}`}
        >
          INIT
        </span>

        {STAGES.map((stage, idx) => {
          const done = activeIndex !== -1 && idx < activeIndex;
          const active = idx === activeIndex;
          return (
            <span
              key={stage}
              className={`${styles.chip} ${active ? styles.chipActive : done ? styles.chipDone : styles.chipTodo}`}
            >
              {done && <span className={styles.chipCheck}>✓</span>}
              {STAGE_LABELS[stage] ?? stage}
            </span>
          );
        })}

        {/* 终态：SUCCESS 绿 / FAILED 红 / REJECTED 橙 */}
        {isTerminal && (
          <span
            className={`${styles.chip} ${status === "SUCCESS" ? styles.chipSuccess : status === "FAILED" ? styles.chipFailed : styles.chipRejected}`}
          >
            {TERMINAL_LABELS[status]}
          </span>
        )}
      </div>
    </div>
  );
}