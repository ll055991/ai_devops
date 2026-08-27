// ---------------------------------------------------------------------------
// 回复文本清洗（任务 2.3 优化）
//
// 职责：剥离混杂在 Agent 回复文本里的"工具结果原始 JSON"。
//
// 背景：部分模型会把工具返回的结构化结果（如 {"success": true, "files": [...]}）
//       原样粘连进回复文本（"{"success": true...}以下是结果"），前端展示很难看。
//       提示词已约束（backend prompts.py），此处做前端兜底。
//
// 策略（保守起见只删"工具结果"，不动其他内容）：
//   1. 按 {} / [] 括号配对扫描文本，取合法 JSON 候选块
//   2. 仅当候选块顶层含布尔 success 字段（后端所有工具结果 _ok/_err 的特征）
//      才整块剔除，其余文本原样保留
//   3. 非法 JSON / 不带 success 的 JSON（可能是正常回复内容）一律保留
// ---------------------------------------------------------------------------

// 从 start 开始找与 text[start] 配对的 JSON 块结束下标（引号/转义感知）
// 找不到配对返回 -1
function findJsonBlockEnd(text: string, start: number): number {
  const open = text[start];
  const close = open === "{" ? "}" : "]";
  let depth = 0;
  let inString = false;
  let escaped = false;

  for (let i = start; i < text.length; i++) {
    const ch = text[i];
    if (inString) {
      if (escaped) {
        escaped = false;
        continue;
      }
      if (ch === "\\") {
        escaped = true;
        continue;
      }
      if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') {
      inString = true;
      continue;
    }
    if (ch === open) {
      depth++;
    } else if (ch === close) {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

// 判断候选块是否为"工具结果 JSON"：顶层对象含布尔 success 字段
// （backend tools/__init__.py 的 _ok/_err 返回结构特征）
function isToolResultJson(parsed: unknown): boolean {
  return (
    typeof parsed === "object" &&
    parsed !== null &&
    "success" in parsed &&
    typeof (parsed as { success: unknown }).success === "boolean"
  );
}

// 主入口：剥离回复文本中混入的工具结果 JSON，返回清洗后的文本
// 例：{"success": true, "count": 2}以下是结果 → 以下是结果
//     非法 JSON 或普通 JSON（无 success 字段）保留原样
export function stripToolResultJson(text: string): string {
  if (text.indexOf("{") === -1 && text.indexOf("[") === -1) return text;

  let out = "";
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "{" || ch === "[") {
      const end = findJsonBlockEnd(text, i);
      if (end !== -1) {
        const candidate = text.slice(i, end + 1);
        try {
          if (isToolResultJson(JSON.parse(candidate))) {
            // 命中工具结果 JSON：整块丢弃，继续扫描后续内容
            i = end + 1;
            continue;
          }
        } catch {
          // 非法 JSON：按普通文本保留
        }
        // 普通对象/数组/非法块：整块保留，不深入内部扫描
        // （避免把数组里的成功对象拆出来，导致 [] 残留）
        out += candidate;
        i = end + 1;
        continue;
      }
    }
    out += ch;
    i++;
  }
  return out.trim();
}