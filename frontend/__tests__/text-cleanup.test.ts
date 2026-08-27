// ---------------------------------------------------------------------------
// 回复文本清洗纯函数测试（任务 2.3 优化）
//
// 覆盖：
//   - 工具结果 JSON 粘连在文字前/后被剥离
//   - success:false 同样剥离
//   - 不带 success 字段的 JSON（正常回复内容）保留
//   - 非法 JSON / 普通文本不动
//   - 多行 JSON、嵌套引号、数组中带 success 的对象
// 运行：npx vitest run
// ---------------------------------------------------------------------------

import { describe, it, expect } from "vitest";
import { stripToolResultJson } from "../lib/text-cleanup";

describe("stripToolResultJson", () => {
  it("剥离粘连在文字前面的工具结果 JSON", () => {
    const text =
      '{"success": true, "workspace": "/root/test0820", "count": 2}工作区当前包含以下内容';
    expect(stripToolResultJson(text)).toBe("工作区当前包含以下内容");
  });

  it("剥离 success:false 的失败结果 JSON", () => {
    expect(stripToolResultJson('{"success": false, "message": "boom"}失败原因')).toBe(
      "失败原因",
    );
  });

  it("纯工具结果 JSON（无后续文字）剥离后为空", () => {
    expect(stripToolResultJson('{"success": true, "count": 2}')).toBe("");
  });

  it("不带 success 字段的 JSON 保留原样", () => {
    const text = '示例 JSON：{"foo": "bar"} 请参考';
    expect(stripToolResultJson(text)).toBe(text);
  });

  it("非法 JSON 保留原样", () => {
    const text = "返回了 {not valid json} 内容";
    expect(stripToolResultJson(text)).toBe(text);
  });

  it("普通文本不动", () => {
    const text = "部署完成，commit 为 a1b2c3d";
    expect(stripToolResultJson(text)).toBe(text);
  });

  it("多行 JSON 同样剥离", () => {
    const text =
      '{\n  "success": true,\n  "files": [{"name": "a.txt"}]\n}\n以下是文件列表';
    expect(stripToolResultJson(text)).toBe("以下是文件列表");
  });

  it("JSON 内的转义引号不影响括号配对", () => {
    const text = '{"success": true, "note": "say \\"hi\\""}后文';
    expect(stripToolResultJson(text)).toBe("后文");
  });

  it("数组整体保留（后端工具结果恒为对象，数组视为普通内容）", () => {
    // 数组含 success 对象也不拆散（避免 [] 残留），普通数组同理
    expect(stripToolResultJson('[{"success": true}]后续')).toBe('[{"success": true}]后续');
    expect(stripToolResultJson('[1, 2, 3] 数字列表')).toBe("[1, 2, 3] 数字列表");
  });

  it("剥离后两端空白收敛", () => {
    expect(stripToolResultJson('  {"success": true}  内容  ')).toBe("内容");
  });
});