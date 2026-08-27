# 自动部署 Agent Demo —— Trae 任务模板（完整版 · 含日志功能）

> 对应需求文档：《自动部署Agent-Demo需求文档.md》
> 版本基线：**deepagents==0.4.12**（与原项目一致，勿升级）
> 目标环境（已确认）：服务器 `10.1.248.143:22(root)`，镜像前缀 `ontology/ontology-graph`；仓库地址与分支由用户在对话中指定（无白名单，鉴权由 `.env` 的 GITLAB_USER/GITLAB_TOKEN 注入）；容器名与 workspace 为多值白名单（见 `.env` 的 CONTAINER_NAMES / WORKSPACES，逗号分隔，扩展只需末尾追加 `,新值` 后重启）

---

## 使用说明

### 公共模板头（每个任务必带）

```text
角色：资深全栈工程师，严谨、不偷懒、不自作主张。
请你完成以下任务
参考项目（只读，禁止修改）：E:\shuzhi_project\ontology-agent-111.tar\ontology-agent-111\app\src\ontology_agent\
约束：
1. 不要新增需求之外的功能，不要"顺便优化"无关代码；
2. 所有工具做参数校验，错误必须转结构化 JSON 返回，不能让 Agent 崩溃；
3. 写代码前先读我指定的参考文件，用"参考其实现方式"而非凭印象；
4. 注释用中文；完成后运行我给的验证命令并汇报结果；若有失败，修复到通过为止；
5. 所有日志走 loguru 全局 logger（logging.py 中定义），禁止 print 调试。
6. 最后给出优化建议和如果有更好的方案请给我备选方案。（优化建议备选方案不要先执行，必须经过我的同意）
```

### 日志功能总体约定（贯穿所有任务）

- **日志三层**：① 后端运行日志（loguru，控制台+文件轮转）；② 前端日志区（build 日志 SSE `log` 事件实时展示）；③ 部署审计记录（JSONL 落盘）。
- **统一格式**：`工具/事件名 | key=value | ...`（参考原项目 `AI_NATIVE_APPROVAL | event=...` 风格）。
- **敏感信息**：密码/token 一律打码后再打日志。

---

# Step 1：后端 MVP（7 个子任务）

## 任务 1.1 项目初始化与配置骨架

```text
【本任务】Step 1 之"初始化骨架"，对应需求文档第八、二章。

创建 agent-deploy-demo/backend/ 目录，完成：
1. pyproject.toml：项目名 deploy-agent，Python >=3.12.9，依赖锁定：
   deepagents==0.4.12、langchain-core==1.4.0、langchain-openai==1.2.1、
   fastapi[standard]>=0.135.2、pydantic-settings、httpx、loguru、uvicorn、
   paramiko>=3.5.0（SSH 执行远程命令）。
   参考 E:\...\ontology-agent-111\app\pyproject.toml 的写法（含 uv index 镜像）。
2. src/deploy_agent/settings.py：
   先读参考项目 src\ontology_agent\settings.py，模仿其写法，字段改为：
   - OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL / OPENAI_TEMPERATURE
   - 仓库地址/分支由用户在对话中指定，不进 settings（无白名单）
   - GITLAB_USER / GITLAB_TOKEN（可选，用于给用户传入的 repo_url 注入鉴权）
   - SERVER_HOST=10.1.248.143 / SERVER_PORT=22 / SERVER_USER=root / SERVER_PASSWORD / SERVER_SSH_KEY(可选)
   - CONTAINER_NAMES=ontology-graph（多值白名单，逗号分隔）、IMAGE_PREFIX=ontology/ontology-graph
   - WORKSPACES=/data/deploy/workspace（多值白名单，逗号分隔）
   - IMAGE_TAG_FORMAT=%Y%m%d-%H%M%S、HEALTH_URL
   - APPROVAL_REQUIRED_TOOLS=stop_container,start_container
   - LOG_LEVEL=INFO、LOG_DIR=logs
   - HOST/PORT（默认 127.0.0.1:8000）
   - model_config 必须加 populate_by_name=True（否则测试用字段名直接构造时 kwarg 被忽略，值回退到 .env）
3. src/deploy_agent/runtime.py：
   参考项目 src\ontology_agent\agent\runtime.py，定义 RuntimeContext：
   container_name、image_tag、environment、user_id 字段 + 上下文渲染函数。
4. 包结构：__init__.py 导出。

【日志功能】新增 src/deploy_agent/logging.py：
   - 统一 loguru 配置：控制台输出 + 文件输出（轮转 50MB、保留 7 份）；
   - 级别由 LOG_LEVEL 控制（默认 INFO）；LOG_DIR 不存在则自动创建；
   - 暴露全局 logger：from deploy_agent.logging import logger；
   - .env 增加 LOG_LEVEL=INFO、LOG_DIR=logs。

验证：uv sync 成功；uv run python -c "from deploy_agent import settings, runtime" 不报错；
uv run python -c "from deploy_agent.logging import logger; logger.info('log ok')" 后
确认 logs/ 目录出现日志文件。
汇报：依赖安装结果 + 配置文件清单 + 日志文件验证结果。
```

## 任务 1.2 Agent 工厂（先不带审批）

```text
【本任务】Step 1 之"Agent 工厂"，对应需求文档第三、四章。

实现 src/deploy_agent/factory.py：
1. build_chat_model(settings)：参考项目 src\ontology_agent\agent\factory.py 开头
   build_chat_model 函数（含 thinking 禁用、max_tokens 参数），用 langchain-openai 的 ChatOpenAI。
2. prompts.py：先写一个初版系统提示词，要点：
   - 角色：软件部署专家；
   - 规则：只能调用提供的 5 个工具，禁止编造参数；
   - 流程：SSH 到目标服务器 → git pull → docker build → (审批后) stop → start → 健康检查；
   - 所有回复用中文。
3. create_deploy_agent(settings, checkpointer=None)：
   参考 create_ai_native_agent（factory.py 1446-1476 行）的写法：
   create_deep_agent(name="deploy-agent", model=..., tools=..., system_prompt=...,
   context_schema=RuntimeContext, checkpointer=InMemorySaver())。
   注意：本任务先不注册审批中间件（下个任务加），工具先传空列表占位。

【日志功能】本任务无需额外日志点（Agent 内部日志由后续任务覆盖）。

验证：uv run python -c "from deploy_agent.factory import create_deploy_agent; a=create_deploy_agent(); print(a.name)"。
汇报：agent 可创建成功。
```

## 任务 1.3 五个部署工具（SSH 远程执行）

```text
【本任务】Step 1 之"业务工具"，对应需求文档第四章。5 个工具全写在 tools.py。
【执行方式】所有工具通过 SSH（paramiko）在目标服务器 10.1.248.143 上执行，
           不在本机直接跑 git/docker。连接信息从 settings 读取，密码打码。

参考项目 src\ontology_agent\agent\tools\management_api.py 的骨架：
（langchain @tool 装饰、参数校验、异常转结构化 JSON、async 实现）

1. git_pull_code(repo_url, branch, workspace)：
   repo_url/branch 由用户在对话中指定，不做白名单校验；
   校验 workspace 必须在 settings.workspaces 白名单内（否则返回错误）；
   鉴权由 _build_repo_url_with_auth(repo_url, settings) 把 GITLAB_USER:GITLAB_TOKEN 注入用户传入的 repo_url；
   SSH 执行：git clone（首次）或 cd + git checkout {branch} + git pull；
   返回 commit hash；
2. build_docker_image(code_path, image_name, image_tag)：
   校验 image_name 以 IMAGE_PREFIX（ontology/ontology-graph）开头；
   SSH 执行 docker build；完整 stdout/stderr 存入返回 JSON 的 log 字段
   （不要流式，一次性返回）；返回 image 全名；
3. stop_container(container_name)：校验在 settings.container_names 白名单内；
   SSH 执行 docker stop；
4. start_container(container_name, image)：校验容器名在白名单内 + 镜像前缀；
   SSH 执行 docker run -d --name ...（复用原端口/网络，若容器已存在先 remove）；
5. check_service_health(container_name, health_url)：
   SSH docker inspect 运行状态 + 服务器上 curl health_url，返回 healthy/http_status。

另写 tools/__init__.py 导出 build_tools(settings) -> list。

【日志功能】每个工具内部日志约定（用全局 logger）：
- 进入时：logger.info("tool={} | args={}", tool_name, 参数摘要（密码/token 打码）)；
- 成功时：logger.info("tool={} | ok | elapsed={}ms", tool_name, 耗时)；
- 失败时：logger.error("tool={} | error={} | elapsed={}ms", tool_name, err, 耗时, exc_info=True)；
- SSH 执行类工具额外记录：目标 host、命令摘要（不含密码）。

验证：uv run pytest -x tests/test_tools.py（你需先建该测试文件）：
- 参数校验分支：错误的 workspace / container_name / image 前缀必须返回失败 JSON；
- SSH 执行：若无法连接真实服务器，用 monkeypatch 模拟 paramiko 执行结果测试
  成功/失败两条路径。
汇报：测试通过清单 + 日志输出示例。
```

## 任务 1.4 审批中间件（本项目核心）

```text
【本任务】Step 1 之"审批机制"，对应需求文档第五章。

实现 src/deploy_agent/middleware.py，先读参考项目 src\ontology_agent\agent\factory.py
中 ConditionalAPIMiddleware（约 692-960 行），完整理解 interrupt()/resume 机制后
改写为 DeployApprovalMiddleware：
1. 触发条件：工具名在 settings.approval_tool_names() 名单中（默认 stop_container,
   start_container），在 after_model 里检查 AIMessage.tool_calls；
2. 命中则 interrupt({"action_requests": [{name, args, description}], "review_configs": [...]})
   ，description 形如 "停止容器 ontology-graph"；
3. resume 值格式 {"decisions": [...]}，支持 type=approve 和 type=reject
   （reject 返回错误 ToolMessage 给模型）；edit 可以不做（Demo 简化）；
4. 同时实现 EnvScopingMiddleware：参考 OntologyIdScopingMiddleware（factory.py
   592-688 行）。当前白名单从单值改为多值（CONTAINER_NAMES/WORKSPACES），
   强制覆盖单一值的语义不再适用，校验逻辑已下沉到各 tool 内部
   （git_pull_code 校验 workspace、stop/start_container 校验 container_name），
   middleware 仅做参数透传，保留类结构不破坏 factory.py 注册链与测试用例。

【日志功能】审批事件日志（参考原项目 AI_NATIVE_APPROVAL | event=... 格式）：
- 触发中断：logger.info("APPROVAL | event=required | thread_id={} | tool={} | args={}",
  thread_id, tool_name, 参数摘要)；
- 收到决策：logger.info("APPROVAL | event=decided | thread_id={} | decision_type={} | tool={}",
  thread_id, decision_type, tool_name)；
- 拒绝时：logger.warning("APPROVAL | event=rejected | thread_id={} | tool={} | reason={}",
  thread_id, tool_name, 拒绝原因)；
- 决策数量不匹配等异常：logger.error(..., exc_info=True)。

验证：uv run pytest -x tests/test_approval.py：
- 构造一次带 stop_container tool_call 的图执行，断言触发 GraphInterrupt；
- 用 Command(resume={"decisions":[{"type":"approve"}]}) 恢复，断言工具被执行；
- reject 断言工具不执行且模型收到错误 ToolMessage；
- 断言日志中出现 APPROVAL | event=required 与 event=decided。
汇报：测试结果 + 日志片段。
```

## 任务 1.5 API 层 + SSE 事件流

```text
【本任务】Step 1 之"API 与 SSE"，对应需求文档第六、七章。

实现 src/deploy_agent/api.py：
1. POST /api/agent/chat，请求体 {message, thread_id?, decisions?}：
   - 无 decisions：agent_input = {"messages":[{"role":"user","content":message}]}；
   - 有 decisions：agent_input = Command(resume={"decisions": decisions})；
   - config = {"configurable": {"thread_id": thread_id}}；
   - 参考项目 src\ontology_agent\api\app.py 的 ai_native_invoke_stream（5550-5700 行）
     的流式翻译方式：agent.astream(..., stream_mode=["messages","updates"])；
2. 事件输出（全部 SSE，event 行 + data 行 JSON）：
   agent_state(running)、tool_call_start、tool_call_end、message_delta(text)、
   log(每 80 字符把 tool 返回的 log 字段切成多条)、approval_required、
   task_status、stream_complete(final_result)、error；
3. task_status 状态推断（工具名→状态映射）：git_pull_code→GIT_PULL、
   build_docker_image→BUILD_IMAGE、stop_container→STOP_CONTAINER、
   start_container→START_CONTAINER、check_service_health→HEALTH_CHECK；
4. 流结束（或被 interrupt 打断）后调 agent.aget_state(config)，若 state.interrupts
   非空则发 approval_required 事件并结束本次流（等待前端带 decisions 重来）；
   否则发 stream_complete。
5. GET /healthz 返回 ok。
入口：uv run uvicorn deploy_agent.api:app --host 127.0.0.1 --port 8000。

【日志功能】
6. FastAPI 请求日志中间件：记录 method / path / status_code / 耗时 / thread_id（有则记）；
7. SSE 事件流日志（参考原项目 AiNativeStreamLogger 思路）：
   - 流开始：logger.info("SSE | event=stream_start | thread_id={} | message={}", ...)；
   - 每个事件发出：logger.debug("SSE | event={} | thread_id={}", ...)（debug 级，防刷屏）；
   - 流结束/中断：logger.info("SSE | event=stream_end | thread_id={} | status={}", ...)；
   - 异常：logger.error("SSE | event=stream_error | thread_id={} | error={}", ..., exc_info=True)。

验证：
1. 写 scripts/smoke_test.py：用 httpx 流式 POST /api/agent/chat，消息用
   "部署仓库 http://10.19.79.176:8190/xxx.git 的 ctc_jt_1.1.1 分支"（仓库/分支由用户在对话中指定），把收到的所有事件打印出来；
2. 手动跑一遍，观察事件顺序是否符合需求文档第六章表格；
3. 检查后端日志：请求日志 + stream_start/stream_end 均已记录。
汇报：事件流文本 + 日志片段。
```

## 任务 1.6 系统提示词 + 部署 SOP Skill

```text
【本任务】Step 1 之"提示词与 Skill"，对应需求文档第四、十二章。

1. 完善 prompts.py 的最终系统提示词，包含：
   - 角色与职责边界（只做部署，不做其他）；
   - 目标环境事实：仓库地址/分支由用户在对话中指定（无白名单）、
     服务器 10.1.248.143、容器名白名单（settings.container_names）、
     镜像前缀 ontology/ontology-graph、workspace 白名单（settings.workspaces）
     （从 runtime/settings 注入，不得在提示词里写死密码）；
   - 指令执行前置校验：每次用户提供指令时，必须先判断是否存在原则性错误，
     有风险时先向用户说明可能导致的后果，等待确认后再执行；
   - 强制规则：调用任何工具前必须读 skills/deployment/SKILL.md 对应小节；
   - 部署顺序约束：build 成功前禁止调用 stop/start；stop/start 触发审批后必须
     等待审批结果，不得自行绕过；
   - 错误处理：工具返回失败时先说明原因，可重试一次，仍失败则如实向用户报告；
   - 全中文回复，不得输出内部思考过程。
2. 创建 skills/deployment/SKILL.md（参考 skills_ai_native/ontology-management/
   SKILL.md 的格式）：每个工具一节，含：参数表/示例/返回示例/常见错误；
   流程节：SSH→git pull→build→审批→stop/start→health check 的 SOP。
3. 在 factory.py 中接入 skills：参考原 factory.py _build_ontology_page_backend
   （FilesystemBackend + CompositeBackend 挂载，1012-1024 行）+ create_deep_agent
   的 skills=[...] 参数。

【日志功能】本任务无日志点（文档内容）。

验证：uv run python -c "from deploy_agent.factory import create_deploy_agent; create_deploy_agent()" 成功；
然后用 curl 发一条消息问"你会按什么顺序部署？"，检查回答是否引用 SKILL 中的 SOP。
汇报：Agent 回答内容。
```

## 任务 1.7 后端整体自测

```text
【本任务】Step 1 收尾，对应需求文档第九、十章验收前 3 项。

1. 通读你写出的 backend 全部代码，自查：白名单校验是否都在工具层生效、
   审批中间件是否只拦 stop/start、SSE 事件是否与需求文档第六章一致；
2. 修复任何不一致；
3. 跑通完整无审批路径：让 Agent 只执行 git_pull_code + build_docker_image
   （提示词明确不做容器操作），确认事件流正常结束 stream_complete；
4. 跑通审批路径：发"部署仓库 http://10.19.79.176:8190/xxx.git 的 ctc_jt_1.1.1 分支"，确认收到 approval_required 后
   用 decisions 恢复，看到 STOP_CONTAINER→START_CONTAINER→HEALTH_CHECK→SUCCESS。
5. 更新 backend/README.md：启动方法、环境变量说明、curl 示例。

【日志功能】验证日志完整性：
6. 跑完后检查 logs/ 目录：
   - app.log 含请求日志、SSE stream_start/stream_end；
   - 工具日志含 5 个工具各自的 tool=xxx | ok/error 记录；
   - 审批路径下含 APPROVAL | event=required / event=decided；
   - 确认日志中无明文密码（grep SERVER_PASSWORD 的值应不存在）。

汇报：两条路径的事件流摘要 + 日志检查结果 + README 内容。
```

---

# Step 2：前端（4 个子任务）

## 任务 2.1 前端初始化 + BFF 代理

```text
【本任务】Step 2 之"前端骨架"，对应需求文档第三、六章。

1. 在 agent-deploy-demo/frontend/ 执行 create-next-app（TS + App Router + Tailwind 可选，
   不做 UI 库依赖，纯 CSS 即可）；
2. 创建 app/api/chat/route.ts（BFF 代理）：
   - POST：接收 {message, threadId, decisions}（前端格式），翻译为后端
     {message, thread_id, decisions} 后转发到后端 /api/agent/chat；
   - 用 node fetch 拿到 SSE 流，原样透传为 Response（ReadableStream）；
   - 后端地址读 env：AGENT_API_URL（默认 http://127.0.0.1:9080，即本地网关）。
   参考项目 ai-native/app/api/ontology-chat/route.ts + config.ts 的思路，
   但 demo 砍掉 convert-request/persist/interceptor（不需要线程映射和 SQLite）。
3. .env.development：AGENT_API_URL=http://127.0.0.1:9080

【日志功能】代理层日志：转发失败/超时时 console.error 输出完整错误；
成功时 console.debug 输出 method/path/status。

验证：npm run dev 后 curl -N http://localhost:3000/api/chat 能看到后端事件透传。
汇报：透传结果。
```

## 任务 2.2 SSE 解析与类型定义

```text
【本任务】Step 2 之"SSE 客户端"，对应需求文档第六章。

1. lib/types.ts：定义与后端一致的 9 种事件类型（agent_state/tool_call_start/
   tool_call_end/message_delta/log/approval_required/task_status/stream_complete/error），
   字段与需求文档第六章表格完全一致；
2. lib/sse.ts：SSE 解析器，参考项目 ai-native/lib/ontology-agent/sse-parser.ts，
   实现：从 fetch body ReadableStream 逐块解码，按 \n\n 切分事件，解析 event/data
   行，回调 onEvent(event)。
3. 写一个纯函数测试（node 单测或页面 debug 面板）验证解析正确性。

【日志功能】解析器内部遇到无法解析的行：console.warn("sse parse skip: ...") 并继续，
不得中断整个流。

验证：npm run test 或控制台输出解析结果。
汇报：测试结果。
```

## 任务 2.3 对话页面组件

```text
【本任务】Step 2 之"页面实现"，对应需求文档第四章。

实现 app/page.tsx（单页即可），拆组件：
1. 消息区：用户消息 + Agent 流式文本（message_delta 边收边渲染），最终文本存本地 state；
2. 工具卡片列表：tool_call_start 插入卡片（图标+工具名+参数折叠），tool_call_end
   更新结果（成功绿勾/失败红叉），参考 ai-native/components/assistant-ui/
   tool-fallback.tsx 的样式思路；
3. 日志区：log 事件追加到滚动容器（docker build 输出），自动滚到底部，带清空按钮；
4. 审批弹窗：收到 approval_required 弹出（actions 逐条列出 name/args/description），
   两个按钮"批准/拒绝"；点击后把 decisions 放进请求体重新 POST /api/chat
   （同 threadId，message 为空），期间显示"等待审批结果"，收到后续事件继续渲染；
5. 状态条：task_status 显示当前阶段（INIT→...→SUCCESS/FAILED/REJECTED）；
6. 发送框：输入 + 发送按钮（Enter 发送），发送中禁用；
7. stream_complete / error 处理：展示最终结果，恢复输入框。

【日志功能】
8. 前端自身错误（fetch 失败/SSE 解析异常）用 console.error 输出，并在日志区顶部
   追加 "[系统] ..." 灰字提示，方便区分前端还是后端问题；
9. 审批弹窗状态变化（弹出/已批准/已拒绝）console.debug 记录。

验证：浏览器手动跑完整流程（含审批批准/拒绝各一次）。
汇报：完整演示的截图或录屏说明。
```

## 任务 2.4 前后端联调

```text
【本任务】Step 2 收尾。

1. 把 ai-native/local-gateway.mjs 原样复制到 agent-deploy-demo/ 根目录，
   BACKEND_URL 默认指向 127.0.0.1:8000，node local-gateway.mjs 启动；
2. 前端 .env.development 的 AGENT_API_URL=http://127.0.0.1:9080；
3. 依次启动：后端 uvicorn → 网关 → 前端 dev；
4. 完整演示三次："部署仓库 http://.../xxx.git 的 ctc_jt_1.1.1 分支"（批准）、
   "部署到容器 not-in-whitelist"（拒绝，容器名不在 CONTAINER_NAMES 白名单）、
   "只拉代码不部署"（验证 Agent 理解能力）；
5. 记录每次的事件序列与最终状态，检查与需求文档验收标准 1-7 对应。

【日志功能】联调时对照三个日志源排查问题：
- 前端浏览器 console（前端错误/[系统] 提示）；
- 后端 logs/app.log（工具/审批/SSE 日志）；
- 部署审计 jsonl（若已加任务 3.2 的内容）。

汇报：三次演示结果 + 验收清单勾选情况。
```

---

# Step 3：打磨（3 个子任务）

## 任务 3.1 提示词与行为迭代

```text
【本任务】Step 3 之"Agent 行为打磨"。

测试以下场景，逐条修正 prompts.py + SKILL.md：
1. 用户说"帮我重新部署"（无仓库/分支信息）→ Agent 应询问仓库地址和分支
   （无默认值，由用户在对话中指定），不能瞎编；
2. 用户说"把 master 部署到生产"→ 分支无白名单，Agent 应直接执行；
   但容器名/workspace 不在白名单时 → Agent 应说明不支持；
3. build 失败 → Agent 应展示错误原因并停止，不继续 stop/start；
4. 健康检查失败 → 输出 Deployment FAILED 及原因，旧容器应保持运行；
5. 长对话中途切换话题 → Agent 应回到部署语境。
每个场景修正后记录改动。

【日志功能】场景测试时同步检查：
- 工具失败场景：后端日志出现 tool=xxx | error=...；
- 拒绝场景：APPROVAL | event=rejected 有记录。

验证：5 个场景各跑一遍。
汇报：场景结果表（通过/修正了什么）+ 日志抽查。
```

## 任务 3.2 异常边界与健壮性

```text
【本任务】Step 3 之"健壮性"。

1. 后端：所有工具 subprocess/SSH 调用加 timeout 与错误码处理；API 层捕获
   GraphInterrupt 之外的异常并返回 error 事件；checkpoint 用 InMemorySaver，
   进程重启后旧 thread 不可恢复，明确在 README 说明（Demo 接受）；
2. 前端：fetch 失败/超时提示；SSE 断流重连提示（不做自动重连，提示即可）；
   审批弹窗重复弹出去重（同 call_id 只弹一次）；
3. 增加 tools 层参数校验的边界用例测试。

【日志功能】新增部署审计记录（JSONL，不建数据库）：
4. 每次部署会话在 logs/deployments/<thread_id>.jsonl 逐行追加：
   启动时间、thread_id、分支、commit、每个工具开始/结束（含结果）、
   审批决策（类型/时间）、最终状态（SUCCESS/FAILED/REJECTED）；
   实现方式：api.py 里收到 tool_call_start/tool_call_end/approval_required/
   stream_complete 时同步写一行（不阻塞主流程）；用 json.dumps 保证单行可解析；
5. logs/ 目录加入 .gitignore。

验证：pytest 全绿；故意停掉后端再发消息，前端有错误提示；
跑一次完整部署（或模拟）后检查 logs/deployments/ 下 jsonl 内容完整。
汇报：修复清单 + 审计文件示例。
```

## 任务 3.3 收尾交付

```text
【本任务】Step 3 之"交付物"。

1. 根目录 README.md：项目简介、架构图（需求文档第三章）、启动三步走命令、
   环境变量表、演示话术 3 条、已知限制（Demo 不做清单）；
2. scripts/demo.sh（或 .ps1）：一键按顺序启动后端/网关/前端并打印端口；
3. 对照《自动部署Agent-Demo需求文档.md》第十章验收清单逐项自测并勾选；
4. 最终代码走查：确认无调试残留、无未用依赖、无注释掉的死代码、无 print 调试
   （应全部使用 loguru）。

【日志功能】
5. README 增加"日志说明"小节：日志位置（logs/app.log、logs/deployments/）、
   级别配置（LOG_LEVEL）、排查指引（前端问题看浏览器 console，后端问题看 app.log）；
6. 交付清单包含：日志功能自测结果（三层日志各截一段示例）。

汇报：验收清单完成情况 + 交付文件清单 + 日志示例。
```

---

# 附录：日志功能清单汇总（三层）

| 层 | 位置 | 内容 | 任务 |
|---|---|---|---|
| ① 后端运行日志 | `logs/app.log`（loguru，轮转） | 请求日志、SSE 流开始/结束、工具执行（参数/耗时/错误）、审批事件、异常堆栈 | 1.1、1.3、1.4、1.5、1.7 |
| ② 前端日志 | 页面日志区 + 浏览器 console | build 日志实时滚动（`log` 事件）、[系统] 错误提示、console.error/debug | 2.1、2.2、2.3、2.4 |
| ③ 部署审计 | `logs/deployments/<thread_id>.jsonl` | 部署全链路事件：时间/分支/commit/工具/审批/最终状态 | 3.2、2.4（联调对照） |

**敏感信息红线**：密码、token 一律打码；`.env` 与 `logs/` 加入 `.gitignore`；验收时 grep 确认日志无明文密码（任务 1.7）。





| 任务       | 验证命令                                                     | 通过标准                                                     |
| ---------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| 1.1 骨架   | `uv sync` + `uv run python -c "from deploy_agent import settings, runtime"` + loguru 测试 | 无报错；`logs/` 目录出现文件                                 |
| 1.2 工厂   | `uv run python -c "from deploy_agent.factory import create_deploy_agent; create_deploy_agent()"` | 打印 agent 名，无异常                                        |
| 1.3 工具   | `uv run pytest -x tests/test_tools.py`                       | 参数校验/成功/失败路径全过                                   |
| 1.4 审批   | `uv run pytest -x tests/test_approval.py`                    | 触发中断、approve 恢复、reject 拦截三项断言过                |
| 1.5 API    | 启动 uvicorn 后 `curl -N -X POST http://127.0.0.1:8000/api/agent/chat -H "Content-Type: application/json" -d '{"message":"部署仓库 http://10.19.79.176:8190/xxx.git 的 ctc_jt_1.1.1 分支"}'` | 终端看到 `agent_state→tool_call→message_delta→…→stream_complete` 完整事件序列 |
| 1.6 提示词 | curl 发"你会按什么顺序部署？"                                | 回答引用 SKILL 的 SOP 顺序                                   |
| 1.7 收尾   | 完整跑一次部署（批准路径）                                   | 事件流到 `HEALTH_CHECK→SUCCESS`；日志含 APPROVAL 事件、无明文密码 |
| 2.1 代理   | `curl -N http://localhost:3000/api/chat`                     | 后端事件穿透到前端端口                                       |
| 2.2 解析   | `npm run test` 或 debug 面板                                 | 9 种事件类型解析正确                                         |
| 2.3 页面   | 浏览器手动发消息                                             | 流式文字/工具卡片/日志滚动/审批弹窗全出现                    |
| 2.4 联调   | 三次演示（批准/拒绝/只拉代码）                               | 三次事件序列与最终状态符合预期                               |
| 3.x 打磨   | 5 场景 + pytest                                              | 场景表全过，pytest 全绿                                      |
