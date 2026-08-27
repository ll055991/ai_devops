"""部署运行时上下文。

- RuntimeContext 用 pydantic BaseModel 承载单次部署任务的关键字段
- coerce_runtime_context 归一化多种输入为 RuntimeContext
- runtime_context_to_config 序列化为可写入 checkpoint 的 dict
- render_runtime_context_block 渲染为可拼入 system prompt 的文本块
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RuntimeContext(BaseModel):
    """单次部署任务的运行时上下文。

    字段说明：
    - container_name: 目标容器名（受白名单约束，Tool 内校验）
    - image_tag: 本次构建出的镜像 tag
    - environment: 部署环境标识（如 dev / test / prod）
    - user_id: 发起部署的用户标识，用于审计与日志
    - additional_info: 额外说明，便于 prompt 渲染
    """

    container_name: str | None = None
    image_tag: str | None = None
    environment: str = "default"
    user_id: str | None = None
    additional_info: str | None = None


def coerce_runtime_context(value: Any) -> RuntimeContext:
    """把 None / dict / BaseModel 统一归一化为 RuntimeContext。"""
    if value is None:
        return RuntimeContext()
    if isinstance(value, RuntimeContext):
        return value
    if hasattr(value, "model_dump"):
        return RuntimeContext.model_validate(value.model_dump(exclude_none=True))
    if isinstance(value, dict):
        return RuntimeContext.model_validate(value)
    return RuntimeContext()


def runtime_context_to_config(value: RuntimeContext | None) -> dict[str, Any] | None:
    """序列化为可写入 LangGraph checkpoint config 的 dict。"""
    if value is None:
        return None
    payload = value.model_dump(exclude_none=True)
    if not payload:
        return None
    return payload


def render_runtime_context_block(value: Any) -> str | None:
    """渲染为可拼入 system prompt 的文本块。无内容时返回 None。"""
    context = coerce_runtime_context(value)
    lines: list[str] = []

    if context.container_name:
        lines.append(f"Container: {context.container_name}")
    if context.image_tag:
        lines.append(f"Image tag: {context.image_tag}")
    if context.environment:
        lines.append(f"Environment: {context.environment}")
    if context.user_id:
        lines.append(f"User: {context.user_id}")

    if not lines:
        return None

    bullets = "\n".join(f"- {line}" for line in lines)
    return f"Current deployment context:\n{bullets}"
