"""PostgreSQL 访问层：建表、快照读取、整网导入、隔离提交、复供确认。"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

import psycopg2
import psycopg2.extras
from psycopg2.errors import UndefinedTable

from .config import get_settings
from .planning import EVENT_PENDING, EVENT_RESTORED, IsolationEvent, Node, Pipe, PlanResult

DDL = """
CREATE TABLE IF NOT EXISTS network_meta (
    id          INTEGER PRIMARY KEY DEFAULT 1,
    version     INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT network_meta_singleton CHECK (id = 1)
);

INSERT INTO network_meta (id, version)
VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS nodes (
    id     TEXT PRIMARY KEY,
    type   TEXT NOT NULL CHECK (type IN ('source', 'sink', 'junction')),
    name   TEXT
);

CREATE TABLE IF NOT EXISTS pipes (
    id           TEXT PRIMARY KEY,
    from_node    TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    to_node      TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    has_valve    BOOLEAN NOT NULL DEFAULT FALSE,
    valve_closed BOOLEAN NOT NULL DEFAULT FALSE,
    name         TEXT,
    CONSTRAINT pipes_no_self_loop CHECK (from_node <> to_node)
);

CREATE INDEX IF NOT EXISTS idx_pipes_from_node ON pipes (from_node);
CREATE INDEX IF NOT EXISTS idx_pipes_to_node ON pipes (to_node);

CREATE TABLE IF NOT EXISTS isolation_events (
    id                BIGSERIAL PRIMARY KEY,
    version_before    INTEGER NOT NULL,
    version_after     INTEGER NOT NULL,
    fault_pipe_id     TEXT NOT NULL,
    closed_pipe_ids   JSONB NOT NULL,
    affected_sink_ids JSONB NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'restored')),
    restored_at       TIMESTAMPTZ,
    restored_version  INTEGER,
    reopened_pipe_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 兼容旧库：为已存在的隔离事件表补齐复供相关列
ALTER TABLE isolation_events ADD COLUMN IF NOT EXISTS status TEXT NOT NULL
    DEFAULT 'pending';
ALTER TABLE isolation_events ADD COLUMN IF NOT EXISTS restored_at TIMESTAMPTZ;
ALTER TABLE isolation_events ADD COLUMN IF NOT EXISTS restored_version INTEGER;
ALTER TABLE isolation_events ADD COLUMN IF NOT EXISTS reopened_pipe_ids JSONB
    NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_isolation_events_status ON isolation_events (status);
"""


@contextmanager
def get_conn() -> Iterator:
    """获取一个数据库连接，调用方负责 commit/rollback。"""
    conn = psycopg2.connect(get_settings().database_url)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator:
    """事务上下文：正常结束提交，异常回滚。"""
    with get_conn() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db() -> None:
    """启动时建表；等待 PostgreSQL 就绪。"""
    settings = get_settings()
    last_error: Exception | None = None
    for attempt in range(1, settings.db_connect_retries + 1):
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(DDL)
                conn.commit()
            return
        except (psycopg2.OperationalError, UndefinedTable) as exc:  # pragma: no cover
            last_error = exc
            time.sleep(settings.db_connect_retry_interval)
    raise RuntimeError(f"数据库初始化失败：{last_error}")


# ---------------------------------------------------------------- 快照


def load_snapshot(conn, *, for_update: bool = False) -> tuple[int, datetime | None, dict[str, Node], dict[str, Pipe]]:
    """读取当前版本与全部节点/管段。for_update=True 时锁定元数据行。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT version, updated_at FROM network_meta WHERE id = 1"
            f"{' FOR UPDATE' if for_update else ''}"
        )
        row = cur.fetchone()
        version = row["version"] if row else 0
        updated_at = row["updated_at"] if row else None

        cur.execute("SELECT id, type, name FROM nodes")
        nodes = {
            r["id"]: Node(id=r["id"], type=r["type"], name=r["name"])
            for r in cur.fetchall()
        }

        cur.execute(
            "SELECT id, from_node, to_node, has_valve, valve_closed, name "
            "FROM pipes ORDER BY id"
        )
        pipes = {
            r["id"]: Pipe(
                id=r["id"],
                from_node=r["from_node"],
                to_node=r["to_node"],
                has_valve=r["has_valve"],
                valve_closed=r["valve_closed"],
                name=r["name"],
            )
            for r in cur.fetchall()
        }
    return version, updated_at, nodes, pipes


# ---------------------------------------------------------------- 整网导入


def replace_network(conn, nodes: list[Node], pipes: list[Pipe]) -> int:
    """在已开启的事务中整网替换：清空旧数据并重写，版本重置为 1。

    返回新版本号。导入是对管网的整体重新定义，因此版本从头计数；
    历史事件也随整网替换一并清空，避免引用旧编号。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM network_meta WHERE id = 1 FOR UPDATE")
        if cur.fetchone() is None:
            cur.execute("INSERT INTO network_meta (id, version) VALUES (1, 0)")

        cur.execute("TRUNCATE isolation_events")
        cur.execute("TRUNCATE pipes")
        cur.execute("TRUNCATE nodes")

        cur.executemany(
            "INSERT INTO nodes (id, type, name) VALUES (%s, %s, %s)",
            [(n.id, n.type, n.name) for n in nodes],
        )
        cur.executemany(
            "INSERT INTO pipes (id, from_node, to_node, has_valve, valve_closed, name) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [
                (p.id, p.from_node, p.to_node, p.has_valve, p.valve_closed, p.name)
                for p in pipes
            ],
        )
        cur.execute(
            "UPDATE network_meta SET version = 1, updated_at = now() WHERE id = 1"
        )
    return 1


# ---------------------------------------------------------------- 隔离提交


def submit_isolation(
    conn,
    expected_version: int,
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    result: PlanResult,
) -> dict:
    """在已开启的事务中完成提交。

    调用方须先用当前快照重新评估并确认 result.isolatable 且无阻断问题。
    本函数只负责：锁行 → 版本比对 → 原子更新阀位、版本 → 写事件。
    返回新版本信息；版本过期抛出 VersionConflict。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM network_meta WHERE id = 1 FOR UPDATE")
        row = cur.fetchone()
        current_version = row[0] if row else 0
        if current_version != expected_version:
            raise VersionConflict(current_version, expected_version)

        if close_pipe_ids:
            cur.execute(
                "UPDATE pipes SET valve_closed = TRUE "
                "WHERE id = ANY(%s) AND has_valve = TRUE",
                (close_pipe_ids,),
            )
        new_version = current_version + 1
        cur.execute(
            "UPDATE network_meta SET version = %s, updated_at = now() WHERE id = 1",
            (new_version,),
        )
        cur.execute(
            "INSERT INTO isolation_events "
            "(version_before, version_after, fault_pipe_id, "
            " closed_pipe_ids, affected_sink_ids) "
            "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb) RETURNING id, created_at",
            (
                current_version,
                new_version,
                fault_pipe_id,
                json.dumps(close_pipe_ids),
                json.dumps(result.affected_sink_ids),
            ),
        )
        event_id, created_at = cur.fetchone()

    return {
        "version": new_version,
        "event_id": event_id,
        "created_at": created_at,
    }


class VersionConflict(Exception):
    def __init__(self, current: int, expected: int):
        self.current = current
        self.expected = expected
        super().__init__(f"版本过期：当前版本 {current}，提交基于版本 {expected}")


# ---------------------------------------------------------------- 隔离事件


def _row_to_event(r) -> dict:
    closed = r["closed_pipe_ids"]
    affected = r["affected_sink_ids"]
    reopened = r.get("reopened_pipe_ids") or []
    # 防御：某些驱动配置下 jsonb 可能返回字符串
    if isinstance(closed, str):
        closed = json.loads(closed)
    if isinstance(affected, str):
        affected = json.loads(affected)
    if isinstance(reopened, str):
        reopened = json.loads(reopened)
    return {
        "id": r["id"],
        "version_before": r["version_before"],
        "version_after": r["version_after"],
        "fault_pipe_id": r["fault_pipe_id"],
        "closed_pipe_ids": closed,
        "affected_sink_ids": affected,
        "status": r.get("status", EVENT_PENDING),
        "restored_at": r.get("restored_at"),
        "restored_version": r.get("restored_version"),
        "reopened_pipe_ids": reopened,
        "created_at": r["created_at"],
    }


def to_domain_event(row: dict) -> IsolationEvent:
    return IsolationEvent(
        id=row["id"],
        fault_pipe_id=row["fault_pipe_id"],
        closed_pipe_ids=tuple(row["closed_pipe_ids"]),
        affected_sink_ids=tuple(row["affected_sink_ids"]),
        status=row["status"],
        version_before=row["version_before"],
        version_after=row["version_after"],
    )


_EVENT_COLUMNS = (
    "id, version_before, version_after, fault_pipe_id, "
    "closed_pipe_ids, affected_sink_ids, status, restored_at, "
    "restored_version, reopened_pipe_ids, created_at"
)


def load_event(conn, event_id: int, *, for_update: bool = False) -> dict | None:
    """按编号读取单个隔离事件；for_update=True 时锁定该行。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_EVENT_COLUMNS} FROM isolation_events WHERE id = %s"
            f"{' FOR UPDATE' if for_update else ''}",
            (event_id,),
        )
        r = cur.fetchone()
    return _row_to_event(r) if r else None


def load_pending_events(conn) -> list[dict]:
    """读取全部待修复（pending）隔离事件。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_EVENT_COLUMNS} FROM isolation_events "
            "WHERE status = 'pending' ORDER BY id"
        )
        rows = cur.fetchall()
    return [_row_to_event(r) for r in rows]


# ---------------------------------------------------------------- 复供确认


def confirm_restoration(
    conn,
    expected_version: int,
    event_id: int,
    reopened_pipe_ids: list[str],
    recoverable_sink_ids: list[str],
) -> dict:
    """在已开启的事务中确认复供（调用方须先完成安全性评估）。

    顺序：锁定版本行并复核版本 → 锁定事件行并复核状态 →
    仅重开“不再被其他待修复事件占用”的阀门 → 版本 +1 → 事件置为已复供。
    任一步失败即抛异常，由外层事务整体回滚，不产生部分变更。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM network_meta WHERE id = 1 FOR UPDATE")
        row = cur.fetchone()
        current_version = row[0] if row else 0
        if current_version != expected_version:
            raise VersionConflict(current_version, expected_version)

        cur.execute(
            "SELECT status FROM isolation_events WHERE id = %s FOR UPDATE",
            (event_id,),
        )
        ev = cur.fetchone()
        if ev is None:
            raise EventNotFound(event_id)
        if ev[0] == EVENT_RESTORED:
            raise EventAlreadyRestored(event_id)

        if reopened_pipe_ids:
            # 只重开确属本事件、且未被其他待修复事件继续占用的阀门
            cur.execute(
                """
                UPDATE pipes SET valve_closed = FALSE
                WHERE id = ANY(%s)
                  AND has_valve = TRUE
                  AND NOT EXISTS (
                      SELECT 1
                      FROM isolation_events e
                      WHERE e.status = 'pending'
                        AND e.id <> %s
                        AND e.closed_pipe_ids ?| %s
                  )
                """,
                (reopened_pipe_ids, event_id, reopened_pipe_ids),
            )

        new_version = current_version + 1
        cur.execute(
            "UPDATE network_meta SET version = %s, updated_at = now() WHERE id = 1",
            (new_version,),
        )
        cur.execute(
            """
            UPDATE isolation_events
            SET status = 'restored',
                restored_at = now(),
                restored_version = %s,
                reopened_pipe_ids = %s::jsonb
            WHERE id = %s AND status = 'pending'
            RETURNING id
            """,
            (new_version, json.dumps(reopened_pipe_ids), event_id),
        )
        if cur.fetchone() is None:  # 并发下被其他事务抢先复供
            raise EventAlreadyRestored(event_id)

    return {
        "version": new_version,
        "event_id": event_id,
        "reopened_pipe_ids": list(reopened_pipe_ids),
        "recoverable_sink_ids": list(recoverable_sink_ids),
    }


class EventNotFound(Exception):
    def __init__(self, event_id: int):
        self.event_id = event_id
        super().__init__(f"隔离事件不存在：{event_id}")


class EventAlreadyRestored(Exception):
    def __init__(self, event_id: int):
        self.event_id = event_id
        super().__init__(f"隔离事件已复供，不能重复操作：{event_id}")


# ---------------------------------------------------------------- 历史


def list_events(conn, limit: int) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_EVENT_COLUMNS} FROM isolation_events "
            "ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()

    return [_row_to_event(r) for r in rows]
