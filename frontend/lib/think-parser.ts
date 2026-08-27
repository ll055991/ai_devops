// ---------------------------------------------------------------------------
// 深度思考标签解析
//
// 职责：从模型输出文本中识别 <think>...</think> 标签，
//       将思考过程与最终回答拆分解耦。
//
// 标签说明：
//   - 部分模型（如 Qwen）会在回复中使用 <think>...</think> 包裹思考链
//   - 思考闭合前为"思考中"态，闭合后为"思考完成"态
//   - 不含 <think> 标签的消息保持原样，不影响现有渲染逻辑
// ---------------------------------------------------------------------------

export interface ThinkParseResult {
  /** 思考内容（<think> 标签内的文本），无思考标签时为 null */
  thought: string | null;
  /** 最终回答（<think> 标签外的正文），无思考标签时等于原始 content */
  finalContent: string;
  /** 是否正在思考中（已出现 <think> 但尚未闭合 </think>） */
  isThinking: boolean;
}

/**
 * 从消息内容中解析 <think>...</think> 标签，拆分思考与回答。
 *
 * 三种情况：
 * a. 流式传输中：已出现 <think> 但未遇 </think> → thought=标签内内容, isThinking=true, finalContent=""
 * b. 思考闭合后：已遇 </think> → thought=标签内内容, isThinking=false, finalContent=标签后正文
 * c. 无思考标签：thought=null, isThinking=false, finalContent=原始内容
 */
export function parseThinkTags(content: string): ThinkParseResult {
  // 模型输出的思考标签为 <think>...</think>（含尖括号）
  const thinkOpen = "<think>";
  const thinkClose = "</think>";

  const openIdx = content.indexOf(thinkOpen);
  if (openIdx === -1) {
    // c. 无思考标签
    return { thought: null, finalContent: content, isThinking: false };
  }

  // 思考内容起始位置（跳过 <think> 标签本身）
  const thoughtStart = openIdx + thinkOpen.length;
  const closeIdx = content.indexOf(thinkClose, thoughtStart);

  if (closeIdx === -1) {
    // a. 思考进行中：只出现了 <think>，还没闭合
    const thought = content.slice(thoughtStart);
    return { thought, finalContent: "", isThinking: true };
  }

  // b. 思考已闭合
  const thought = content.slice(thoughtStart, closeIdx);
  const finalContent = content.slice(closeIdx + thinkClose.length);
  return { thought, finalContent, isThinking: false };
}
