"""部署状态存储（DeploymentState）。

对应改造方案阶段 1「部署状态存储」：
- SQLite 落盘（backend/checkpoints/deployments.db，与对话记忆同目录，已被 .gitignore 覆盖）
- deployments 表按 thread_id 关联一次部署：commit / image / container / environment / status
- 状态机：pending → building → deploying → healthy | unhealthy | failed（rolled_back 供阶段 2 回滚使用）
- DeploymentStateStore 提供 record（upsert）/ get_by_thread / list_history
- get_deployment_store 单例 + close_deployment_store 供 api.py lifespan 关闭连接

实现方式参考：
- factory.build_default_checkpointer（aiosqlite 连接 + 目录自动创建 + 文件落盘）
- settings.get_settings（lru_cache 单例）
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

# 项目根目录（backend/）
# parents[0]=deploy_agent, parents[1]=src, parents[2]=backend
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 部署状态数据库目录与文件（与 checkpoints.db 同目录，该目录已被 .gitignore 忽略）
_STATE_DIR = _PROJECT_ROOT / "checkpoints"
_STATE_DB = _STATE_DIR / "deployments.db"

# 部署状态合法取值（状态机）
DEPLOYMENT_STATUSES = (
    "pending",
    "building",
    "deploying",
    "healthy",
    "unhealthy",
    "failed",
    "rolled_back",
)

# deployments 表可写入字段白名单（record 的 kwargs 只允许这些 key，防止拼 SQL 注入）
_ALLOWED_FIELDS = {
    "repo_url",
    "branch",
    "commit",
    "image",
    "container",
    "environment",
    "status",
    "error",
    "rollback_from",
}

# sqlite3.execute 一次只支持单条语句，建表与建索引必须分开执行
# 注意：commit 是 SQLite 保留字（事务命令），列名必须用双引号转义，
# 否则 CREATE TABLE 报 near "commit": syntax error
_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    repo_url TEXT,
    branch TEXT,
    "commit" TEXT,
    image TEXT,
    container TEXT,
    environment TEXT,
    status TEXT NOT NULL,
    error TEXT,
    rollback_from INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
_SCHEMA_INDEX = """
CREATE INDEX IF NOT EXISTS idx_deployments_container
    ON deployments(container, updated_at)
"""


def _now() -> str:
    """当前时间的 ISO 字符串（秒级精度，落盘用）。"""
    return datetime.now().isoformat(timespec="seconds")


def _q(name: str) -> str:
    """SQL 标识符转义：双引号包裹列名（commit 是 SQLite 保留字，必须转义）。"""
    return f'"{name}"'


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    """把查询结果行转换为 dict。"""
    keys = [
        "id",
        "thread_id",
        "repo_url",
        "branch",
        "commit",
        "image",
        "container",
        "environment",
        "status",
        "error",
        "rollback_from",
        "created_at",
        "updated_at",
    ]
    return {key: row[idx] for idx, key in enumerate(keys)}


class DeploymentStateStore:
    """部署状态 SQLite 存储。

    单连接（aiosqlite）复用模式，与 AsyncSqliteSaver 一致：
    - 首次使用时惰性建库建表
    - 所有写操作后 commit
    - 进程退出时由调用方显式 close（api.py lifespan）
    """

    def __init__(self, db_path: Path | str):
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def _get_conn(self) -> aiosqlite.Connection:
        """惰性连接：目录不存在自动创建，首次连接时建表。"""
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(self._db_path))
            await conn.execute(_SCHEMA_TABLE)
            await conn.execute(_SCHEMA_INDEX)
            await conn.commit()
            self._conn = conn
        return self._conn

    async def close(self) -> None:
        """关闭连接（幂等）。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def record(
        self, thread_id: str, **fields: Any
    ) -> dict[str, Any]:
        """记录一次部署活动（按 thread_id upsert）。

        - 记录不存在：以 status=pending 创建，再应用非 None 字段
        - 记录已存在：仅更新非 None 字段 + updated_at
        - fields 只允许 _ALLOWED_FIELDS 内的 key，非法 key 直接忽略并告警

        Returns:
            更新后的部署记录 dict
        """
        conn = await self._get_conn()

        # 字段白名单过滤：防拼 SQL 注入（key 来自代码内部映射，双保险）
        safe_fields: dict[str, Any] = {}
        for key, value in fields.items():
            if key in _ALLOWED_FIELDS:
                if value is not None:
                    safe_fields[key] = value
            else:
                logger.warning(
                    "STATE | event=unknown_field_ignored | thread_id={} | field={}",
                    thread_id,
                    key,
                )

        now = _now()
        rows = await conn.execute_fetchall(
            "SELECT id FROM deployments WHERE thread_id = ?", (thread_id,)
        )

        if not rows:
            # 新记录：pending 起步，再合并 safe_fields
            merged = {"status": "pending", **safe_fields}
            merged.setdefault("status", "pending")
            columns = ["thread_id", "status", "created_at", "updated_at"]
            values: list[Any] = [thread_id, merged["status"], now, now]
            for key in _ALLOWED_FIELDS - {"status"}:
                if key in merged and merged[key] is not None:
                    columns.append(key)
                    values.append(merged[key])
            placeholders = ", ".join("?" for _ in columns)
            await conn.execute(
                f"INSERT INTO deployments ({', '.join(_q(c) for c in columns)}) "
                f"VALUES ({placeholders})",
                values,
            )
            await conn.commit()
            logger.info(
                "STATE | event=deployment_created | thread_id={} | status={}",
                thread_id,
                merged["status"],
            )
        else:
            # 已存在：只更新 safe_fields + updated_at
            if safe_fields:
                assignments = ", ".join(f"{_q(key)} = ?" for key in safe_fields)
                values = list(safe_fields.values()) + [now, thread_id]
                await conn.execute(
                    f"UPDATE deployments SET {assignments}, updated_at = ? "
                    f"WHERE thread_id = ?",
                    values,
                )
                await conn.commit()
            else:
                # 无新字段也刷新 updated_at，保证活跃线程排序正确
                await conn.execute(
                    "UPDATE deployments SET updated_at = ? WHERE thread_id = ?",
                    (now, thread_id),
                )
                await conn.commit()

        return await self.get_by_thread(thread_id)  # type: ignore[return-value]

    async def get_by_thread(self, thread_id: str) -> dict[str, Any] | None:
        """按 thread_id 查询最新部署记录，不存在返回 None。"""
        conn = await self._get_conn()
        rows = await conn.execute_fetchall(
            "SELECT * FROM deployments WHERE thread_id = ? LIMIT 1",
            (thread_id,),
        )
        if not rows:
            return None
        return _row_to_dict(rows[0])

    async def get_by_id(self, deployment_id: int) -> dict[str, Any] | None:
        """按主键 id 查询部署记录（回滚目标查询用），不存在返回 None。"""
        conn = await self._get_conn()
        rows = await conn.execute_fetchall(
            "SELECT * FROM deployments WHERE id = ? LIMIT 1",
            (int(deployment_id),),
        )
        if not rows:
            return None
        return _row_to_dict(rows[0])

    async def latest_by_container(self, container: str) -> dict[str, Any] | None:
        """按容器名查询最近一条部署记录（RiskControl 前置校验用），不存在返回 None。"""
        conn = await self._get_conn()
        rows = await conn.execute_fetchall(
            "SELECT * FROM deployments WHERE container = ? "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (container,),
        )
        if not rows:
            return None
        return _row_to_dict(rows[0])

    async def list_history(
        self, container: str = "", limit: int = 20
    ) -> list[dict[str, Any]]:
        """查询部署历史（按更新时间倒序）。

        Args:
            container: 非空时只返回该容器的记录
            limit: 返回条数上限（1~100，超出按 100 处理）
        """
        conn = await self._get_conn()
        safe_limit = max(1, min(int(limit), 100))
        if container:
            rows = await conn.execute_fetchall(
                "SELECT * FROM deployments WHERE container = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (container, safe_limit),
            )
        else:
            rows = await conn.execute_fetchall(
                "SELECT * FROM deployments ORDER BY updated_at DESC, id DESC LIMIT ?",
                (safe_limit,),
            )
        return [_row_to_dict(row) for row in rows]


@lru_cache(maxsize=1)
def get_deployment_store() -> DeploymentStateStore:
    """单例获取部署状态存储（默认 backend/checkpoints/deployments.db）。

    测试场景直接构造 DeploymentStateStore(tmp_path / "x.db")，不走单例。
    """
    return DeploymentStateStore(_STATE_DB)


async def close_deployment_store() -> None:
    """关闭单例存储的连接（api.py lifespan 退出时调用）。"""
    store = get_deployment_store()
    try:
        await store.close()
    except Exception as exc:
        logger.warning("STATE | event=store_close_failed | error={}", str(exc))


__all__ = [
    "DeploymentStateStore",
    "get_deployment_store",
    "close_deployment_store",
    "DEPLOYMENT_STATUSES",
]