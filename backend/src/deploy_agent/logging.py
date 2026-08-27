"""统一日志配置。

基于 loguru 提供：
- 控制台输出（stderr，彩色，级别由 LOG_LEVEL 控制）
- 文件输出（LOG_DIR/deploy_agent.log，轮转 50MB、保留 7 份，UTF-8）
- LOG_DIR 不存在则自动创建
- 暴露全局 logger：from deploy_agent.logging import logger

设计说明：
- 直接复用 loguru 原生 logger（全局单例，不重新实例化）
- 模块导入时自动配置一次（_CONFIGURED 幂等标志位）
- 读取 LOG_LEVEL/LOG_DIR 时做降级：优先 settings，失败退回环境变量+默认值
  （避免 settings 加载失败拖垮日志，保证 logger 永远可用）
- LOG_DIR 为相对路径时基于项目根目录（backend/）解析，便于日志落在项目目录下
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

# 项目根目录（backend/）
# parents[0]=deploy_agent, parents[1]=src, parents[2]=backend
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 幂等标志位：防止重复 add 导致日志重复输出
_CONFIGURED = False

# 默认值（与 .env 一致，作为 settings 加载失败时的兜底）
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_DIR = "logs"


def _resolve_log_level() -> str:
    """读取 LOG_LEVEL，优先 settings，失败退回环境变量 + 默认值。"""
    try:
        from deploy_agent.settings import get_settings

        return get_settings().log_level
    except Exception:
        # settings 加载失败不影响日志可用性，退回环境变量 + 默认值
        return os.environ.get("LOG_LEVEL", _DEFAULT_LOG_LEVEL)


def _resolve_log_dir() -> Path:
    """读取 LOG_DIR 并解析为绝对路径（相对路径基于项目根目录）。"""
    try:
        from deploy_agent.settings import get_settings

        log_dir = get_settings().log_dir
    except Exception:
        log_dir = os.environ.get("LOG_DIR", _DEFAULT_LOG_DIR)

    path = Path(log_dir)
    # 相对路径基于项目根目录解析，避免随 cwd 变化
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


def configure_logging() -> None:
    """配置 loguru：控制台 + 文件输出。

    幂等：重复调用不会重复 add handler。
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = _resolve_log_level().upper()
    log_dir = _resolve_log_dir()

    # LOG_DIR 不存在则自动创建（parents=True 允许嵌套创建）
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "deploy_agent.log"

    # 清空 loguru 默认 handler，避免控制台重复输出
    logger.remove()

    # 控制台输出：stderr，彩色，级别由 LOG_LEVEL 控制
    logger.add(
        sys.stderr,
        level=log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
    )

    # 文件输出：轮转 50MB，保留 7 份，UTF-8，级别同 LOG_LEVEL
    logger.add(
        log_file,
        level=log_level,
        rotation="50 MB",
        retention=7,
        encoding="utf-8",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{name}:{function}:{line} - {message}"
        ),
    )

    _CONFIGURED = True


# 模块导入时自动配置一次
configure_logging()

__all__ = ["logger", "configure_logging"]
