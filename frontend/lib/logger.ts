// ---------------------------------------------------------------------------
// 简化日志器
// 参考 ai-native/lib/logger.ts 的 createLogger(module) 作用域思路，
// 但不引入 pino / rotating-file-stream —— 任务模板要求前端日志走 console + 日志区，
// 后端运行日志（loguru）由 backend 自己负责，前端不重复造文件轮转。
//
// 输出格式：[ISO时间] [LEVEL] [module] message {payload}
// 级别控制：dev 下 debug 及以上，prod 下 info 及以上
// ---------------------------------------------------------------------------

const isDev = process.env.NODE_ENV !== "production";

type LogLevel = "trace" | "debug" | "info" | "warn" | "error";

// 数字越大级别越高；低于阈值的日志被丢弃
const LEVELS: Record<LogLevel, number> = {
  trace: 10,
  debug: 20,
  info: 30,
  warn: 40,
  error: 50,
};

// 当前生效的最低级别：开发期debug全开，生产期只留info及以上
const MIN_LEVEL: LogLevel = isDev ? "debug" : "info";

// payload 序列化：序列化失败时不抛错（日志本身不能拖垮主流程）
function formatPayload(payload?: Record<string, unknown>): string {
  if (!payload || Object.keys(payload).length === 0) return "";
  try {
    return " " + JSON.stringify(payload);
  } catch {
    return " [unserializable payload]";
  }
}

// 统一输出入口：用 switch 调用对应 console 方法，规避 console[level] 的类型断言问题
function emit(
  level: LogLevel,
  module: string,
  msg: string,
  payload?: Record<string, unknown>,
): void {
  if (LEVELS[level] < LEVELS[MIN_LEVEL]) return;
  const ts = new Date().toISOString();
  const line = `[${ts}] [${level.toUpperCase()}] [${module}] ${msg}${formatPayload(payload)}`;
  switch (level) {
    case "trace":
    case "debug":
      console.debug(line);
      break;
    case "info":
      console.info(line);
      break;
    case "warn":
      console.warn(line);
      break;
    case "error":
      console.error(line);
      break;
  }
}

export interface Logger {
  trace: (msg: string, payload?: Record<string, unknown>) => void;
  debug: (msg: string, payload?: Record<string, unknown>) => void;
  info: (msg: string, payload?: Record<string, unknown>) => void;
  warn: (msg: string, payload?: Record<string, unknown>) => void;
  error: (msg: string, payload?: Record<string, unknown>) => void;
}

/**
 * 创建模块作用域 logger
 * 用法：const log = createLogger("sse-parser"); log.info("parsed", { type });
 */
export function createLogger(module: string): Logger {
  return {
    trace: (m, p) => emit("trace", module, m, p),
    debug: (m, p) => emit("debug", module, m, p),
    info: (m, p) => emit("info", module, m, p),
    warn: (m, p) => emit("warn", module, m, p),
    error: (m, p) => emit("error", module, m, p),
  };
}

export default createLogger;
