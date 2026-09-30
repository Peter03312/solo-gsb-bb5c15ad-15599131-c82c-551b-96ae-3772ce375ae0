"""供水管网故障隔离服务 —— FastAPI 入口。

接口前缀 /api：
- GET  /health                         健康检查
- POST /api/network/import             整网导入（替换式，版本重置为 1）
- GET  /api/network                    读取当前管网与版本
- POST /api/isolation/preview          隔离方案预演（只读）
- POST /api/isolation/submit           提交隔离（原子关阀 + 版本 +1）
- GET  /api/isolation/events           隔离操作历史（区分待修复/已复供）
- POST /api/isolation/events/{id}/restore/preview  复供预演（只读）
- POST /api/isolation/events/{id}/restore/confirm  确认复供（原子重开阀 + 版本 +1）
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import db
from .config import get_settings
from .planning import (
    Node,
    Pipe,
    RESTORE_REWATER_RISK,
    evaluate_plan,
    evaluate_restoration,
    validate_import,
    BLOCKING_ISSUE_CODES,
)
from .db import to_domain_event
from .schemas import (
    ImportIn,
    PlanIn,
    RestoreIn,
    ImportOut,
    NetworkOut,
    PreviewOut,
    SubmitOut,
    RestorePreviewOut,
    RestoreConfirmOut,
    EventOut,
)
from .serializers import serialize_preview, serialize_restore_preview, serialize_snapshot


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, issues: list | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.issues = issues or []
        super().__init__(message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="供水管网故障隔离服务",
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request, exc: ApiError):
    body: dict = {"error": {"code": exc.code, "message": exc.message}}
    if exc.issues:
        body["error"]["issues"] = [i.as_dict() for i in exc.issues]
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(db.VersionConflict)
async def version_conflict_handler(request, exc: db.VersionConflict):
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "VERSION_CONFLICT",
                "message": f"版本过期：当前版本为 {exc.current}，操作基于版本 {exc.expected}，请重新读取后再试",
                "current_version": exc.current,
                "expected_version": exc.expected,
            }
        },
    )


@app.exception_handler(db.EventNotFound)
async def event_not_found_handler(request, exc: db.EventNotFound):
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "EVENT_NOT_FOUND",
                "message": f"隔离事件不存在：{exc.event_id}",
                "event_id": exc.event_id,
            }
        },
    )


@app.exception_handler(db.EventAlreadyRestored)
async def event_already_restored_handler(request, exc: db.EventAlreadyRestored):
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "EVENT_ALREADY_RESTORED",
                "message": f"隔离事件 {exc.event_id} 已复供，不能重复操作",
                "event_id": exc.event_id,
            }
        },
    )


@app.get("/health", tags=["系统"])
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 管网


def _to_domain_nodes(payload: ImportIn) -> list[Node]:
    return [Node(id=n.id, type=n.type, name=n.name) for n in payload.nodes]


def _to_domain_pipes(payload: ImportIn) -> list[Pipe]:
    return [
        Pipe(
            id=p.id,
            from_node=p.from_node,
            to_node=p.to_node,
            has_valve=p.has_valve,
            valve_closed=p.valve_closed,
            name=p.name,
        )
        for p in payload.pipes
    ]


@app.post(
    "/api/network/import",
    response_model=ImportOut,
    tags=["管网"],
    summary="整网导入（替换当前管网，版本重置为 1，旧隔离事件全部失效）",
)
def import_network(payload: ImportIn):
    nodes = _to_domain_nodes(payload)
    pipes = _to_domain_pipes(payload)

    issues = validate_import(nodes, pipes)
    if issues:
        raise ApiError(
            422,
            "IMPORT_INVALID",
            "管网数据校验失败：存在悬空端点、重号、自环或无阀管段被标记关闭",
            issues=issues,
        )

    with db.transaction() as conn:
        version = db.replace_network(conn, nodes, pipes)

    return {
        "version": version,
        "node_count": len(nodes),
        "pipe_count": len(pipes),
    }


@app.get("/api/network", response_model=NetworkOut, tags=["管网"], summary="读取当前管网与版本")
def get_network():
    with db.get_conn() as conn:
        version, updated_at, nodes, pipes = db.load_snapshot(conn)
    return serialize_snapshot(version, updated_at, nodes, pipes)


# ---------------------------------------------------------------- 隔离


def _load_pending_domain_events(conn) -> list:
    return [to_domain_event(r) for r in db.load_pending_events(conn)]


@app.post(
    "/api/isolation/preview",
    response_model=PreviewOut,
    tags=["故障隔离"],
    summary="预演：判断能否隔离并计算失供用水点（只读，不改状态；计入待修复故障管段）",
)
def preview_isolation(payload: PlanIn):
    with db.get_conn() as conn:
        version, _updated_at, nodes, pipes = db.load_snapshot(conn)
        pending_events = _load_pending_domain_events(conn)

    result = evaluate_plan(
        nodes,
        pipes,
        payload.fault_pipe_id,
        list(payload.close_pipe_ids),
        for_submit=False,
        pending_events=pending_events,
    )
    return serialize_preview(
        version, payload.version, payload.fault_pipe_id, payload.close_pipe_ids, result
    )


@app.post(
    "/api/isolation/submit",
    response_model=SubmitOut,
    tags=["故障隔离"],
    summary="提交隔离：基于当前版本重新校验，原子更新阀位与版本",
)
def submit_isolation(payload: PlanIn):
    close_ids = list(dict.fromkeys(payload.close_pipe_ids))  # 去重但保序（重复仍会被拒）

    with db.transaction() as conn:
        # 锁定当前版本后基于最新数据重新校验
        version, _updated_at, nodes, pipes = db.load_snapshot(conn, for_update=True)

        if version != payload.version:
            raise db.VersionConflict(version, payload.version)

        pending_events = _load_pending_domain_events(conn)

        result = evaluate_plan(
            nodes,
            pipes,
            payload.fault_pipe_id,
            list(payload.close_pipe_ids),
            for_submit=True,
            pending_events=pending_events,
        )

        blocking = [i for i in result.issues if i.code in BLOCKING_ISSUE_CODES]
        if blocking:
            raise ApiError(
                422,
                "PLAN_INVALID",
                "隔离方案非法（管段不存在、无阀管段、重复关闭或把故障管段当作阀门）",
                issues=blocking,
            )
        if not result.isolatable:
            raise ApiError(
                422,
                "NOT_ISOLATABLE",
                "该方案无法隔离故障管段：故障管段至少有一端在方案图中仍可从水源到达",
                issues=result.issues,
            )

        outcome = db.submit_isolation(
            conn,
            expected_version=payload.version,
            fault_pipe_id=payload.fault_pipe_id,
            close_pipe_ids=close_ids,
            result=result,
        )

    return {
        "version": outcome["version"],
        "fault_pipe_id": payload.fault_pipe_id,
        "closed_pipe_ids": close_ids,
        "affected_sink_ids": result.affected_sink_ids,
        "event_id": outcome["event_id"],
    }


@app.get(
    "/api/isolation/events",
    response_model=list[EventOut],
    tags=["故障隔离"],
    summary="隔离操作历史（区分待修复 pending 与已复供 restored，按时间倒序）",
)
def get_events():
    settings = get_settings()
    with db.get_conn() as conn:
        return db.list_events(conn, settings.history_limit)


# ---------------------------------------------------------------- 复供


def _get_pending_event_or_404(conn, event_id: int, *, for_update: bool = False):
    """读取事件：不存在 -> 404；已复供 -> 409。"""
    row = db.load_event(conn, event_id, for_update=for_update)
    if row is None:
        raise db.EventNotFound(event_id)
    if row["status"] == "restored":
        raise db.EventAlreadyRestored(event_id)
    return row


@app.post(
    "/api/isolation/events/{event_id}/restore/preview",
    response_model=RestorePreviewOut,
    tags=["恢复供水"],
    summary="复供预演：返回该事件所关阀门、预计恢复用水点与阻断原因（只读，不改状态）",
)
def restore_preview(event_id: int, payload: RestoreIn):
    with db.get_conn() as conn:
        version, _updated_at, nodes, pipes = db.load_snapshot(conn)
        row = _get_pending_event_or_404(conn, event_id)
        pending_events = [to_domain_event(r) for r in db.load_pending_events(conn)]

    event = to_domain_event(row)
    result = evaluate_restoration(nodes, pipes, event, pending_events)
    return serialize_restore_preview(version, payload.version, event, result)


@app.post(
    "/api/isolation/events/{event_id}/restore/confirm",
    response_model=RestoreConfirmOut,
    tags=["恢复供水"],
    summary="确认复供：同事务复核版本与安全性，重开阀门、更新事件状态与管网版本",
)
def restore_confirm(event_id: int, payload: RestoreIn):
    with db.transaction() as conn:
        # 锁定版本行后，基于最新数据完整复核
        version, _updated_at, nodes, pipes = db.load_snapshot(conn, for_update=True)
        if version != payload.version:
            raise db.VersionConflict(version, payload.version)

        row = _get_pending_event_or_404(conn, event_id, for_update=True)
        event = to_domain_event(row)

        pending_rows = db.load_pending_events(conn)
        pending_events = [to_domain_event(r) for r in pending_rows]
        result = evaluate_restoration(nodes, pipes, event, pending_events)

        if not result.safe:
            risky = [i for i in result.issues if i.code == RESTORE_REWATER_RISK]
            raise ApiError(
                422,
                "RESTORE_UNSAFE",
                "复供被拒绝：重开阀门会使其他待修复故障管段重新进水",
                issues=risky or result.issues,
            )

        outcome = db.confirm_restoration(
            conn,
            expected_version=payload.version,
            event_id=event_id,
            reopened_pipe_ids=result.reopened_pipe_ids,
            recoverable_sink_ids=result.recoverable_sink_ids,
        )

    return {
        "version": outcome["version"],
        "event_id": event_id,
        "status": "restored",
        "fault_pipe_id": event.fault_pipe_id,
        "reopened_pipe_ids": outcome["reopened_pipe_ids"],
        "recoverable_sink_ids": outcome["recoverable_sink_ids"],
        "still_blocked_sink_ids": result.still_blocked_sink_ids,
    }
