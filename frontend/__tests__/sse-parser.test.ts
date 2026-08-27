// ---------------------------------------------------------------------------
// SSE 解析器纯函数测试（任务 2.2 验证项）
//
// 测试范围：
//   - 9 种事件类型解析正确
//   - 心跳行 : keepalive 跳过
//   - 未知事件 / 坏 JSON 跳过不中断
//   - parseSseStream 流式分块边界（事件被切断时仍能正确解析）
//   - 流结束后的 tail 残留块
//
// 运行：npx vitest run
// ---------------------------------------------------------------------------

import { describe, it, expect } from "vitest";
import { parseEventBlock, parseSseStream } from "../lib/sse-parser";
import type { SseEvent } from "../lib/types";

// 辅助：把字符串编码成单块 ReadableStream<Uint8Array>
function toStream(text: string): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(text));
      controller.close();
    },
  });
}

// 辅助：按指定字节分块（模拟真实网络流，事件可能被切断）
function toChunkedStream(text: string, chunkSize: number): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  const bytes = encoder.encode(text);
  return new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += chunkSize) {
        controller.enqueue(bytes.subarray(i, i + chunkSize));
      }
      controller.close();
    },
  });
}

// 辅助：跑完流并收集所有事件
async function collectEvents(stream: ReadableStream<Uint8Array>): Promise<SseEvent[]> {
  const events: SseEvent[] = [];
  await parseSseStream(stream, (e) => events.push(e));
  return events;
}

// ---------------------------------------------------------------------------
// 9 种事件类型解析（对齐 backend/api.py 的 _sse_event 调用点字段）
// ---------------------------------------------------------------------------
describe("parseEventBlock - 9 种事件类型", () => {
  it("agent_state: running/awaiting_approval/completed", () => {
    const block = `event: agent_state\ndata: {"thread_id":"t-1","status":"running"}`;
    expect(parseEventBlock(block)).toEqual({
      type: "agent_state",
      thread_id: "t-1",
      status: "running",
    });
  });

  it("message_delta: LLM 文本增量", () => {
    const block = `event: message_delta\ndata: {"thread_id":"t-1","text":"hello"}`;
    expect(parseEventBlock(block)).toEqual({
      type: "message_delta",
      thread_id: "t-1",
      text: "hello",
    });
  });

  it("tool_call_start: 工具调用开始", () => {
    const block = `event: tool_call_start\ndata: {"thread_id":"t-1","name":"git_pull_code","args":{"repo_url":"http://x"},"call_id":"c1","hint":"正在拉取代码..."}`;
    expect(parseEventBlock(block)).toEqual({
      type: "tool_call_start",
      thread_id: "t-1",
      name: "git_pull_code",
      args: { repo_url: "http://x" },
      call_id: "c1",
      hint: "正在拉取代码...",
    });
  });

  it("tool_call_end: result 为结构化对象", () => {
    const block = `event: tool_call_end\ndata: {"thread_id":"t-1","name":"git_pull_code","result":{"success":true,"log":"ok"},"call_id":"c1"}`;
    expect(parseEventBlock(block)).toEqual({
      type: "tool_call_end",
      thread_id: "t-1",
      name: "git_pull_code",
      result: { success: true, log: "ok" },
      call_id: "c1",
    });
  });

  it("tool_call_end: result 为原始字符串（后端 JSON.parse 失败透传）", () => {
    // 后端 api.py 第 387 行：JSONDecodeError 时 result_data = content（原始字符串）
    const block = `event: tool_call_end\ndata: {"thread_id":"t-1","name":"x","result":"raw-text","call_id":"c1"}`;
    expect(parseEventBlock(block)).toEqual({
      type: "tool_call_end",
      thread_id: "t-1",
      name: "x",
      result: "raw-text",
      call_id: "c1",
    });
  });

  it("task_status: 阶段流转", () => {
    const block = `event: task_status\ndata: {"thread_id":"t-1","status":"GIT_PULL","tool":"git_pull_code"}`;
    expect(parseEventBlock(block)).toEqual({
      type: "task_status",
      thread_id: "t-1",
      status: "GIT_PULL",
      tool: "git_pull_code",
    });
  });

  it("log: 80 字符切片", () => {
    const block = `event: log\ndata: {"thread_id":"t-1","tool":"build_docker_image","text":"Step 1/10"}`;
    expect(parseEventBlock(block)).toEqual({
      type: "log",
      thread_id: "t-1",
      tool: "build_docker_image",
      text: "Step 1/10",
    });
  });

  it("build_log: 实时构建日志", () => {
    const block = `event: build_log\ndata: {"thread_id":"t-1","tool":"build_docker_image","text":"#4 [3/4] RUN pip install"}`;
    expect(parseEventBlock(block)).toEqual({
      type: "build_log",
      thread_id: "t-1",
      tool: "build_docker_image",
      text: "#4 [3/4] RUN pip install",
    });
  });

  it("approval_required: action_requests 数组", () => {
    const block = `event: approval_required\ndata: {"thread_id":"t-1","action_requests":[{"name":"stop_container","args":{"container_name":"x"},"description":"停止容器 x"}],"review_configs":[{"action_name":"stop_container","allowed_decisions":["approve","reject"]}]}`;
    expect(parseEventBlock(block)).toEqual({
      type: "approval_required",
      thread_id: "t-1",
      action_requests: [
        { name: "stop_container", args: { container_name: "x" }, description: "停止容器 x" },
      ],
      review_configs: [
        { action_name: "stop_container", allowed_decisions: ["approve", "reject"] },
      ],
    });
  });

  it("stream_complete: 流结束", () => {
    const block = `event: stream_complete\ndata: {"thread_id":"t-1","final_result":"部署完成"}`;
    expect(parseEventBlock(block)).toEqual({
      type: "stream_complete",
      thread_id: "t-1",
      final_result: "部署完成",
    });
  });

  it("error: 异常", () => {
    const block = `event: error\ndata: {"thread_id":"t-1","message":"boom"}`;
    expect(parseEventBlock(block)).toEqual({
      type: "error",
      thread_id: "t-1",
      message: "boom",
    });
  });
});

// ---------------------------------------------------------------------------
// 心跳与容错
// ---------------------------------------------------------------------------
describe("心跳与容错", () => {
  it("跳过 : keepalive 注释行（心跳）", () => {
    expect(parseEventBlock(": keepalive")).toBeNull();
    // 即使注释行混在事件块中间也应被跳过
    const block = `: keepalive\nevent: agent_state\ndata: {"thread_id":"t-1","status":"running"}`;
    expect(parseEventBlock(block)).toEqual({
      type: "agent_state",
      thread_id: "t-1",
      status: "running",
    });
  });

  it("未登记的 event 被跳过（返回 null，不抛错）", () => {
    const block = `event: unknown_event\ndata: {"foo":"bar"}`;
    expect(parseEventBlock(block)).toBeNull();
  });

  it("坏 JSON 被跳过（返回 null，不抛错）", () => {
    const block = `event: agent_state\ndata: {not valid json}`;
    expect(parseEventBlock(block)).toBeNull();
  });

  it("空块返回 null", () => {
    expect(parseEventBlock("")).toBeNull();
    expect(parseEventBlock("   ")).toBeNull();
  });

  it("只有 event 行没有 data 行返回 null", () => {
    expect(parseEventBlock("event: agent_state")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// parseSseStream 流式解析
// ---------------------------------------------------------------------------
describe("parseSseStream 流式解析", () => {
  it("单次读取多个事件", async () => {
    const text = [
      `event: agent_state\ndata: {"thread_id":"t-1","status":"running"}`,
      "",
      `event: message_delta\ndata: {"thread_id":"t-1","text":"hi"}`,
      "",
      `event: stream_complete\ndata: {"thread_id":"t-1","final_result":"done"}`,
      "",
    ].join("\n");

    const events = await collectEvents(toStream(text));
    expect(events).toHaveLength(3);
    expect(events[0].type).toBe("agent_state");
    expect(events[1].type).toBe("message_delta");
    expect(events[2].type).toBe("stream_complete");
  });

  it("事件被分块切断仍能正确解析（buffer 边界）", async () => {
    // 构造一个完整事件流，按 8 字节切块（一定会在事件中间断开）
    const eventBlock = `event: message_delta\ndata: {"thread_id":"t-1","text":"hello"}\n\n`;
    const stream = toChunkedStream(eventBlock, 8);
    const events = await collectEvents(stream);
    expect(events).toHaveLength(1);
    expect(events[0]).toEqual({
      type: "message_delta",
      thread_id: "t-1",
      text: "hello",
    });
  });

  it("心跳行不影响后续事件解析", async () => {
    const text = [
      ": keepalive",
      "",
      `event: agent_state\ndata: {"thread_id":"t-1","status":"running"}`,
      "",
      ": keepalive",
      "",
    ].join("\n");

    const events = await collectEvents(toStream(text));
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("agent_state");
  });

  it("流结束时的 tail 残留块（无 \\n\\n 结尾）仍被解析", async () => {
    // 最后一块没有 \n\n 结尾，解析器应在流结束后处理
    const text = `event: error\ndata: {"thread_id":"t-1","message":"tail"}`;
    const events = await collectEvents(toStream(text));
    expect(events).toHaveLength(1);
    expect(events[0]).toEqual({
      type: "error",
      thread_id: "t-1",
      message: "tail",
    });
  });

  it("空流不产生事件", async () => {
    const events = await collectEvents(toStream(""));
    expect(events).toEqual([]);
  });

  it("活动超时：无数据时取消流并抛错（不无限等待）", async () => {
    // 模拟一个永不产生数据、也不关闭的流（如后端挂起）
    const silentStream = new ReadableStream<Uint8Array>({
      start() {
        // 故意不 enqueue 也不 close
      },
    });
    await expect(
      parseSseStream(silentStream, () => {}, undefined, 30),
    ).rejects.toThrow(/无数据.*超时/);
  });

  it("完整审批恢复时序：多个事件按顺序回调", async () => {
    // 模拟一次 stop_container 审批批准后的 SSE 序列
    const text = [
      `event: agent_state\ndata: {"thread_id":"t-1","status":"running"}`,
      "",
      `event: tool_call_end\ndata: {"thread_id":"t-1","name":"stop_container","result":{"success":true},"call_id":"c1"}`,
      "",
      `event: task_status\ndata: {"thread_id":"t-1","status":"STOP_CONTAINER","tool":"stop_container"}`,
      "",
      `event: stream_complete\ndata: {"thread_id":"t-1","final_result":"done"}`,
      "",
    ].join("\n");

    const events = await collectEvents(toStream(text));
    expect(events.map((e) => e.type)).toEqual([
      "agent_state",
      "tool_call_end",
      "task_status",
      "stream_complete",
    ]);
  });
});
