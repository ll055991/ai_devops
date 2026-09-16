# ADR-0005：多 SubAgent 拆分（第一阶段）

- 日期：2026-09-15
- 状态：已接受（第一阶段：高风险上收 Main Agent）
- 会话：grill-with-docs

## 背景

单 Agent 挂 20 个工具已跑通全流程。SKILL.md 与系统提示词持续膨胀，提出按 Code / Build / Deploy / Monitor 拆 4 个 SubAgent 做上下文隔离。

## 决策

1. **调度：主流程串行，只有无依赖只读检查可并行**：`code-agent → build-agent → 主 Agent 亲调停旧删旧起新 → monitor-agent`，经 `task()` 委派。全并行否决：commit → image → container 硬依赖。
2. **工具主责 + 只读共享**：Code（拉码·Dockerfile·文件只读）、Build（构建·镜像列表）、deploy-planner（只读规划，不执行）、Monitor（健康·日志·容器·状态，只读）。`list_containers` 等只读工具可共享。
3. **Tool Safety Boundary 第一阶段 = 高风险上收**：审批名单内写操作工具只在主 Agent 手里，复用主层 `after_model` 审批 + Audit/Risk/State 全链路。子 Agent 各装审批中间件否决：改动大，中断冒泡与 SSE 审批恢复链路要重做。
4. **Monitor 一次性检查，三过才成功**：容器 running + HTTP 200 + 日志无 ERROR。
5. **回滚默认自动 + 人工决策并存**：失败默认调 `rollback_deployment`，仍进审批闸（用户可在审批窗拒绝）；用户明确说停则只报告不回滚。

## 后果

- 第二阶段（子 Agent 各自审批 / 审批工具化）待第一阶段跑顺后再议。
- `interrupt()` 只能在 `after_model` 约束不变（见 AGENTS.md）。
