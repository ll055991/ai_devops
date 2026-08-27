// ---------------------------------------------------------------------------
// 审批弹窗（任务 2.3 重点）
//
// 职责：收到 approval_required 后弹出，逐条列出 action_requests 的
//       name / args / description；两个按钮「批准 / 拒绝」。
//       点击后由父组件把 decisions 放进请求体重发 POST /api/chat
//       （同 threadId，message 为空），期间 waiting=true 显示「等待审批结果」。
//
// 说明：
//   - args 用原生 <details> 折叠展示，避免引入额外状态
//   - 决策数量 = action_requests 数量（后端 middleware 校验数量必须匹配，
//     参考 backend/src/deploy_agent/middleware.py 的 decision_mismatch 检查）
//   - 弹窗状态变化（弹出 / 已批准 / 已拒绝）的 console.debug 由 page.tsx 记录
// ---------------------------------------------------------------------------

"use client";

import type { ActionRequest, ReviewConfig } from "@/lib/types";
import styles from "./ApprovalDialog.module.css";

// 审批弹窗数据（approval_required 事件解析而来）
export interface ApprovalData {
  threadId: string;
  actionRequests: ActionRequest[];
  reviewConfigs: ReviewConfig[];
}

// 决策类型（与后端 decisions 字段对齐）
export type ApprovalDecisionType = "approve" | "reject";

interface ApprovalDialogProps {
  data: ApprovalData;
  // true：已提交决策，正在等待后端恢复流（按钮禁用，显示等待文案）
  waiting: boolean;
  onDecide: (decisions: Array<{ type: ApprovalDecisionType }>) => void;
}

export default function ApprovalDialog({ data, waiting, onDecide }: ApprovalDialogProps) {
  const handleDecide = (type: ApprovalDecisionType) => {
    if (waiting) return;
    // 每条 action_request 对应一条决策（与后端中断项数量保持一致）
    const decisions = data.actionRequests.map(() => ({ type }));
    onDecide(decisions);
  };

  return (
    <div className={styles.overlay} role="dialog" aria-modal="true" aria-label="审批请求">
      <div className={styles.dialog}>
        <h2 className={styles.title}>审批请求</h2>
        <p className={styles.subtitle}>Agent 请求执行以下操作，请逐项确认：</p>

        <ul className={styles.actions}>
          {data.actionRequests.map((action, idx) => (
            <li key={`${action.name}-${idx}`} className={styles.action}>
              <div className={styles.actionHeader}>
                <span className={styles.actionName}>{action.name}</span>
                <span className={styles.actionIndex}>#{idx + 1}</span>
              </div>
              <p className={styles.actionDesc}>{action.description}</p>
              {Object.keys(action.args).length > 0 && (
                <details className={styles.args}>
                  <summary>参数</summary>
                  <pre className={styles.argsPre}>{JSON.stringify(action.args, null, 2)}</pre>
                </details>
              )}
            </li>
          ))}
        </ul>

        {waiting ? (
          <p className={styles.waiting}>等待审批结果…</p>
        ) : (
          <div className={styles.buttons}>
            <button
              type="button"
              className={`${styles.btn} ${styles.btnReject}`}
              onClick={() => handleDecide("reject")}
            >
              拒绝
            </button>
            <button
              type="button"
              className={`${styles.btn} ${styles.btnApprove}`}
              onClick={() => handleDecide("approve")}
            >
              批准
            </button>
          </div>
        )}
      </div>
    </div>
  );
}