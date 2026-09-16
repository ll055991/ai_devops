"""操作审计存储（AuditLog）。

对应改造方案阶段 3「AuditLogMiddleware」：
- SQLite 落盘（backend/checkpoints/audit.db，与部署状态同目录，已被 .gitignore 忽略）
- audit_log 表记录每次工具调用：thread_id / 工具名 / 风险等级 / 打码参数摘要 /
  状态（ok|failed|rejected|blocked|error）/ 结果摘要 / 耗时
- 审批决策（approve/reject）由 DeployApprovalMiddleware 补记，状态为 rejected
- AuditLogStore 提供 record / list（thread_id、工具、时间过滤）

实现方式参考 state.DeploymentStateStore（aiosqlite 单连接 + 惰性建表 + 幂等 close）。
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

# 项目根目录（backend/）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 审计数据库路径（与 deployments.db 同目录）
_AUDIT_DIR = _PROJECT_ROOT / "checkpoints"
_AUDIT_DB = _AUDIT_DIR / "audit.db"

# 审计状态合法取值
AUDIT_STATUSES = ("ok", "failed", "rejected", "blocked", "error")

# 风险等级合法取值（低/中/高）
RISK_LEVELS = ("low", "medium", "high")

# sqlite3.execute 一次只支持单条语句，建表与建索引必须分开执行
_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT,
    tool_name TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    args_summary TEXT,
    status TEXT NOT NULL,
    result_summary TEXT,
    elapsed_ms INTEGER,
    created_at TEXT NOT NULL
)
"""
_SCHEMA_INDEX_THREAD = """
CREATE INDEX IF NOT EXISTS idx_audit_thread
    ON audit_log(thread_id, created_at)
"""
_SCHEMA_INDEX_TOOL = """
CREATE INDEX IF NOT EXISTS idx_audit_tool
    ON audit_log(tool_name, created_at)
"""


def _now() -> str:
    """当前时间的 ISO 字符串（秒级精度，落盘用）。"""
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    """把查询结果行转换为 dict。"""
    keys = [
        "id",
        "thread_id",
        "tool_name",
        "risk_level",
        "args_summary",
        "status",
        "result_summary",
        "elapsed_ms",
        "created_at",
    ]
    return {key: row[idx] for idx, key in enumerate(keys)}


class AuditLogStore:
    """操作审计 SQLite 存储（单连接，与 DeploymentStateStore 同模式）。"""

    def __init__(self, db_path: Path | str):
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def _get_conn(self) -> aiosqlite.Connection:
        """惰性连接：目录不存在自动创建，首次连接时建表。"""
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(self._db_path))
            await conn.execute(_SCHEMA_TABLE)
            await conn.execute(_SCHEMA_INDEX_THREAD)
            await conn.execute(_SCHEMA_INDEX_TOOL)
            await conn.commit()
            self._conn = conn
        return self._conn

    async def close(self) -> None:
        """关闭连接（幂等）。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def record(
        self,
        tool_name: str,
        *,
        thread_id: str | None = None,
        risk_level: str = "low",
        args_summary: str | None = None,
        status: str = "ok",
        result_summary: str | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        """追加一条审计记录。字段校验失败时记录日志但不阻断调用。"""
        if risk_level not in RISK_LEVELS:
            logger.warning("AUDIT | event=invalid_risk_level | level={}", risk_level)
            risk_level = "low"
        if status not in AUDIT_STATUSES:
            logger.warning("AUDIT | event=invalid_status | status={}", status)
            status = "error"

        conn = await self._get_conn()
        await conn.execute(
            "INSERT INTO audit_log "
            "(thread_id, tool_name, risk_level, args_summary, status, result_summary, elapsed_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                tool_name,
                risk_level,
                args_summary,
                status,
                result_summary,
                elapsed_ms,
                _now(),
            ),
        )
        await conn.commit()
        logger.debug(
            "AUDIT | event=recorded | thread_id={} | tool={} | status={}",
            thread_id,
            tool_name,
            status,
        )

    async def list(
        self,
        *,
        thread_id: str | None = None,
        tool_name: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """查询审计记录（按时间倒序）。

        Args:
            thread_id: 非空时按 thread 过滤
            tool_name: 非空时按工具名过滤
            limit: 返回条数上限（1~200，超出按 200 处理）
        """
        conn = await self._get_conn()
        safe_limit = max(1, min(int(limit), 200))
        conditions: list[str] = []
        values: list[Any] = []
        if thread_id:
            conditions.append("thread_id = ?")
            values.append(thread_id)
        if tool_name:
            conditions.append("tool_name = ?")
            values.append(tool_name)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(safe_limit)
        rows = await conn.execute_fetchall(
            f"SELECT * FROM audit_log {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            values,
        )
        return [_row_to_dict(row) for row in rows]


@lru_cache(maxsize=1)
def get_audit_store() -> AuditLogStore:
    """单例获取审计存储（默认 backend/checkpoints/audit.db）。

    测试场景直接构造 AuditLogStore(tmp_path / "x.db")，不走单例。
    """
    return AuditLogStore(_AUDIT_DB)


async def close_audit_store() -> None:
    """关闭单例审计存储的连接（api.py lifespan 退出时调用）。"""
    store = get_audit_store()
    try:
        await store.close()
    except Exception as exc:
        logger.warning("AUDIT | event=store_close_failed | error={}", str(exc))


__all__ = [
    "AuditLogStore",
    "get_audit_store",
    "close_audit_store",
    "AUDIT_STATUSES",
    "RISK_LEVELS",
]