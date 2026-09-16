# AGENTS.md

给 AI 编码代理的项目说明。人读文档见 [README.md](README.md)、[架构分析文档.md](架构分析文档.md)、[项目运转流程.md](项目运转流程.md)。

## 项目结构

- `backend/` — FastAPI + DeepAgents/LangGraph，Python ≥3.12，依赖用 `uv` 管理
- `frontend/` — Next.js 16 App Router，BFF 代理 + 对话工作台（前端专属规则见 `frontend/AGENTS.md`）

## 常用命令

### 后端（在 `backend/` 下执行）

```powershell
uv sync                                  # 安装依赖
.venv\Scripts\python -m uvicorn deploy_agent.api:app --host 127.0.0.1 --port 8000
```

测试（**必须带 --ignore**，见下方坑 1；evals 真调 LLM 单独跑）：

```powershell
# 单元套件（快，无外部依赖）
.venv\Scripts\python -m pytest tests -q --ignore=tests/test_approve.py --ignore=tests/test_sse.py --ignore=tests/test_check_container_and_images.py --ignore=tests/evals

# Evals 门禁（慢，真调小模型，需 OPENAI_API_KEY，基线写 .scratch/m1-evals/baseline.md）
.venv\Scripts\python -m pytest tests/evals -q -m gate
```

### 前端（在 `frontend/` 下执行）

```powershell
npm install
npm run dev      # http://localhost:3000
npm test         # vitest run
npm run lint
```

## 关键坑

1. **`pytest tests` 全量收集会失败**：`tests/test_approve.py`、`test_sse.py`、`test_check_container_and_images.py` 是手工 httpx 脚本（不是测试），模块导入即向 `127.0.0.1:8001` 发请求；后端没起就 collection error。跑测试务必加 `--ignore`。`tests/test_dockerfile.py` 是 0 字节空文件。
2. **中文路径下不要用 `uv run uvicorn` 或 `.venv\Scripts\uvicorn.exe`**：uv trampoline 无法解析脚本路径，用 `python -m uvicorn`。
3. **`npm` 必须在 `frontend/` 下执行**，仓库根目录没有 `package.json`。

## 架构不变量（改动前先读）

- **中间件顺序**：`factory.py` 的 `middleware=[...]` 列表**第一个在最外层**（langchain `_chain_tool_call_wrappers` 语义）。当前：`AuditLog → RiskControl → EnvScoping → DeploymentState → DeployApproval`。AuditLog 必须在最外层才能捕获 RiskControl 的拒绝；RiskControl 必须在 DeploymentState 之外，拒绝时短路、不落部署状态。
- **审批 `interrupt()` 只能在 `after_model` / `aafter_model`**，不能在 `wrap_tool_call`——ToolNode 会静默吞掉那里的 `GraphInterrupt`。
- **thread_id 统一用 `_thread_id_from_runtime`**（`middleware.py`）：优先 `runtime.execution_info.thread_id`，回退 `runtime.context` 字典。不要直接读 `runtime.context`（生产里它是 `RuntimeContext` 模型，不是 dict）。
- **工具永不 raise**：统一返回结构化 JSON `{"success": bool, "error_type": ..., "message": ...}`，LLM 靠它决策。白名单校验在最外层工具入口做。
- **链内 handler 返回的是 ToolMessage**：中间件里解析工具结果前必须经 `_unwrap_tool_result` 取 `.content`；直接按 str 解析会导致健康检查恒 unhealthy、失败落不上库、审计全 ok。单元测试的 mock handler 也必须返回 ToolMessage 形态，否则是错误 seam。
- **中间件与工具都从 `settings` 闭包注入依赖**，不要引全局状态。

## 数据与状态

- `state.py` → `deployments` 表（部署状态，`DeploymentStateMiddleware` 在工具成功后 upsert）
- `audit.py` → `audit_log` 表（`AuditLogMiddleware` 记 ok/failed/error，审批 reject 记 rejected）
- 落盘位置 `backend/checkpoints/`（已 gitignore）
- **测试里不要用 `get_*_store()` 单例**，注入 `tmp_path` 临时库（参考 `tests/test_state.py`、`tests/test_approval.py`）

## Agent skills

### Issue tracker

Local markdown（`.scratch/<feature>/spec.md`），本机无 `gh`。见 `docs/agents/issue-tracker.md`。

### Domain docs

单上下文。决策看 `docs/adr/`，术语看 `docs/词汇表.md`，路线看 `docs/路线图.md`。

## 约定

- 文档、注释、日志文案统一中文，减少不必要的注释，保持一致的风格；日志格式 `key=value | key=value`，敏感字段打码
- 提交前跑通上面的后端 pytest 与 `npm test`；不要提交 `.env`（`.env.example` 除外）、`checkpoints/`、`logs/`
- 改动跨度大或与文档不符时，同步更新对应 `.md`（文档常滞后于代码，冲突时以代码为准）
