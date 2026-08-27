// ---------------------------------------------------------------------------
// 后端记忆同步客户端测试（方案 B/C）
//
// 覆盖：
//   - 404 → 抛 ThreadNotFoundError（方案 C 续聊降级依据）
//   - 后端 success:false / 网络失败 → 抛普通 Error
//   - toFrontendMessages：bk- id、空文本过滤、streaming 复位
//   - toTimestamp：ISO 解析与非法值兜底
// 运行：npx vitest run
// ---------------------------------------------------------------------------

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchBackendThreadMessages,
  fetchBackendThreads,
  ThreadNotFoundError,
  toFrontendMessages,
  toTimestamp,
} from "../lib/history-client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchBackendThreadMessages", () => {
  it("404 → ThreadNotFoundError（续聊降级依据）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ success: false, error: "线程不存在: t-1" }), {
          status: 404,
        }),
      ),
    );
    await expect(fetchBackendThreadMessages("t-1")).rejects.toBeInstanceOf(ThreadNotFoundError);
  });

  it("后端 success:false → 普通 Error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ success: false, error: "boom" }), { status: 500 }),
      ),
    );
    await expect(fetchBackendThreadMessages("t-1")).rejects.toThrow("boom");
  });

  it("正常返回消息列表", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ success: true, messages: [{ role: "user", content: "hi" }] })),
        ),
    );
    await expect(fetchBackendThreadMessages("t-1")).resolves.toEqual([
      { role: "user", content: "hi" },
    ]);
  });
});

describe("fetchBackendThreads", () => {
  it("后端 success:false → 普通 Error", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ success: false, error: "boom" }), { status: 500 }),
        ),
    );
    await expect(fetchBackendThreads()).rejects.toThrow("boom");
  });

  it("正常返回线程列表", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            success: true,
            threads: [{ thread_id: "t-1", title: "部署", message_count: 2, updated_at: "2026-08-24T00:00:00Z" }],
          }),
        ),
      ),
    );
    const threads = await fetchBackendThreads();
    expect(threads).toHaveLength(1);
    expect(threads[0].thread_id).toBe("t-1");
  });

  it("非 2xx 无 JSON → 抛错", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("bad gateway", { status: 502 })));
    await expect(fetchBackendThreads()).rejects.toThrow(/502/);
  });
});

describe("toFrontendMessages", () => {
  it("生成 bk- 前缀 id，streaming 复位为 false", () => {
    const msgs = [
      { role: "user" as const, content: "你好" },
      { role: "assistant" as const, content: "在的" },
    ];
    const out = toFrontendMessages(msgs, "t-9");
    expect(out).toEqual([
      { id: "bk-t-9-0", role: "user", content: "你好", streaming: false },
      { id: "bk-t-9-1", role: "assistant", content: "在的", streaming: false },
    ]);
  });

  it("空文本消息被过滤", () => {
    const out = toFrontendMessages(
      [
        { role: "user" as const, content: "" },
        { role: "assistant" as const, content: "有内容" },
      ],
      "t-1",
    );
    expect(out).toHaveLength(1);
    expect(out[0].content).toBe("有内容");
  });
});

describe("toTimestamp", () => {
  it("ISO 字符串 → 毫秒", () => {
    expect(toTimestamp("2026-08-24T00:00:00Z")).toBe(Date.parse("2026-08-24T00:00:00Z"));
  });

  it("非法值退回当前时间", () => {
    const before = Date.now();
    const ts = toTimestamp("not-a-date");
    expect(ts).toBeGreaterThanOrEqual(before);
    expect(ts).toBeLessThanOrEqual(Date.now());
  });
});