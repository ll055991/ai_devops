// ---------------------------------------------------------------------------
// SSE 事件类型定义（任务 2.2）
//
// 字段来源：backend/src/deploy_agent/api.py 的 _sse_event 调用点，严格对齐
// 设计原则：
//   - 9 种事件用联合类型 SseEvent 表达，前端按 event.type 做 switch 收窄
//   - tool_call_end.result 设为 unknown：后端 JSON.parse 成功为结构化对象，
//     失败则透传原始字符串，由 ToolCard 组件运行时按 result.success 收窄
//   - task_status.status 用 string 而非字面量联合：后端枚举可能扩展，
//     前端不强耦合，避免后端新增状态导致编译失败
//   - 心跳行 : keepalive 不建模为事件类型，由 sse-parser 内部丢弃
// ---------------------------------------------------------------------------

// Agent 运行状态变化
// status 取自 api.py 第 269/481/504 行的三种字面量值
export interface AgentStateEvent {
  type: "agent_state";
  thread_id: string;
  status: "running" | "awaiting_approval" | "completed";
}

// LLM 文本增量，前端流式追加到当前 Agent 消息气泡
export interface MessageDeltaEvent {
  type: "message_delta";
  thread_id: string;
  text: string;
}

// 工具调用开始：LLM 决定调用工具时触发
// hint 来自后端 TOOL_HINT_MAP，用于 UI 友好提示
export interface ToolCallStartEvent {
  type: "tool_call_start";
  thread_id: string;
  name: string;
  args: Record<string, unknown>;
  call_id: string;
  hint: string;
}

// 工具调用结束：携带工具执行结果
// result 为 unknown 的原因见文件头注释
export interface ToolCallEndEvent {
  type: "tool_call_end";
  thread_id: string;
  name: string;
  result: unknown;
  call_id: string;
}

// 任务阶段流转：后端 TOOL_STATUS_MAP 把工具名映射为阶段枚举
// status 用 string 类型，便于后端扩展巡检工具的新阶段
export interface TaskStatusEvent {
  type: "task_status";
  thread_id: string;
  status: string;
  tool: string;
}

// 工具返回的 log 字段切片（后端 _split_log_to_chunks 按 80 字符切）
// 前端追加到日志区并自动滚动
export interface LogEvent {
  type: "log";
  thread_id: string;
  tool: string;
  text: string;
}

// 实时构建日志：docker build 每一行输出（后端 _run_ssh_stream 逐行推送）
// 前端追加到日志区实时滚动展示构建过程
export interface BuildLogEvent {
  type: "build_log";
  thread_id: string;
  tool: string;
  text: string;
}

// 单条审批请求：后端 interrupt.value.action_requests 的元素
export interface ActionRequest {
  name: string;
  args: Record<string, unknown>;
  description: string;
}

// 单条审批配置：后端 interrupt.value.review_configs 的元素
export interface ReviewConfig {
  action_name: string;
  allowed_decisions: string[];
}

// 需要人工审批：弹窗逐条展示 action_requests 的 description
// 用户点批准/拒绝后，用同 thread_id + decisions 重新 POST /api/chat
export interface ApprovalRequiredEvent {
  type: "approval_required";
  thread_id: string;
  action_requests: ActionRequest[];
  review_configs: ReviewConfig[];
}

// 流正常结束：显示最终结果，恢复发送框
export interface StreamCompleteEvent {
  type: "stream_complete";
  thread_id: string;
  final_result: string;
}

// 流异常：error.message 是异常字符串
export interface ErrorEvent {
  type: "error";
  thread_id: string;
  message: string;
}

// 10 种事件的联合类型
export type SseEvent =
  | AgentStateEvent
  | MessageDeltaEvent
  | ToolCallStartEvent
  | ToolCallEndEvent
  | TaskStatusEvent
  | LogEvent
  | BuildLogEvent
  | ApprovalRequiredEvent
  | StreamCompleteEvent
  | ErrorEvent;
