"""管网故障隔离 / 复供的纯领域逻辑（不依赖数据库 / Web 框架）。

规则概述：
- 管网为无向图，节点分为水源(source)/用水点(sink)/连接点(junction)。
- 已关闭阀门的管段视为不可通行。
- 其他**待修复**隔离事件的故障管段同样视为不可通行（故障仍在，未通水）。
- 隔离方案图 = 当前管网移除故障管段（及其他待修复故障管段），
  再移除/关闭所有“拟关闭管段”，同时已关闭阀门继续保持关闭。
- 只有故障管段**两端**在方案图中都无法从任一水源到达，方案才可隔离(isolatable)。
- 失供用水点 = 方案前由任一水源可达、方案后不可达的 sink，按编号排序。

复供规则概述：
- 待修复(pending)事件可预演/确认复供；已复供(restored)事件拒绝再次复供。
- 复供方案图 = 移除其他待修复事件的故障管段与**其余仍关闭的阀门**，
  本事件的阀门按重开计、本事件的故障管段视为已修复（恢复通行）。
- 预计恢复用水点 = 复供方案前不可达、方案后可达的 sink，按编号排序。
- 仅当重开后，其余待修复事件的故障管段**两端**仍均无法从水源到达
  （无重新进水风险），才允许确认复供。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque

# 问题代码 ---------------------------------------------------------------

# 阻断类问题：出现任一即不能隔离、提交时拒绝（422）
FAULT_PIPE_NOT_FOUND = "FAULT_PIPE_NOT_FOUND"
CLOSE_PIPE_NOT_FOUND = "CLOSE_PIPE_NOT_FOUND"
CLOSE_PIPE_NO_VALVE = "CLOSE_PIPE_NO_VALVE"
CLOSE_PIPE_DUPLICATE = "CLOSE_PIPE_DUPLICATE"
CLOSE_PIPE_IS_FAULT = "CLOSE_PIPE_IS_FAULT"
# 复供类问题：重开会让其他待修复故障管段某端重新进水
RESUPPLY_RISK = "RESUPPLY_RISK"
# 导入期问题
DUPLICATE_NODE_ID = "DUPLICATE_NODE_ID"
DUPLICATE_PIPE_ID = "DUPLICATE_PIPE_ID"
PIPE_SELF_LOOP = "PIPE_SELF_LOOP"
PIPE_ENDPOINT_UNKNOWN = "PIPE_ENDPOINT_UNKNOWN"
NON_VALVE_PIPE_CLOSED = "NON_VALVE_PIPE_CLOSED"

# 结构性阻断问题（预演会给出，提交据此拒绝）
BLOCKING_ISSUE_CODES = frozenset(
    {
        FAULT_PIPE_NOT_FOUND,
        CLOSE_PIPE_NOT_FOUND,
        CLOSE_PIPE_NO_VALVE,
        CLOSE_PIPE_DUPLICATE,
        CLOSE_PIPE_IS_FAULT,
    }
)


@dataclass(frozen=True)
class Node:
    id: str
    type: str  # source / sink / junction
    name: str | None = None


@dataclass(frozen=True)
class Pipe:
    id: str
    from_node: str
    to_node: str
    has_valve: bool = False
    valve_closed: bool = False
    name: str | None = None


@dataclass
class Issue:
    code: str
    message: str
    pipe_id: str | None = None

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "pipe_id": self.pipe_id}


@dataclass
class PlanResult:
    isolatable: bool
    affected_sink_ids: list[str]
    issues: list[Issue] = field(default_factory=list)
    # 方案中实际被阻断的管段（已关 ∪ 拟关，故障管段单独移除）
    blocked_pipe_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class IsolationEvent:
    """一条隔离事件的计算视图（仅复供计算需要的字段）。"""

    id: int
    fault_pipe_id: str
    closed_pipe_ids: tuple[str, ...]
    status: str = "pending"  # pending 待修复 / restored 已复供


@dataclass
class RestorationResult:
    # 重开本事件阀门后，其余待修复故障管段两端是否仍全部脱离水源
    safe: bool
    # 预计恢复供水的用水点（前不可达、后可达的 sink）
    restored_sink_ids: list[str]
    issues: list[Issue] = field(default_factory=list)
    # 复供后仍不可通行的管段（其余待修复故障管段 ∪ 其余仍关闭阀门）
    blocked_pipe_ids: frozenset[str] = field(default_factory=frozenset)


# ---------------------------------------------------------------- 导入校验


def validate_import(
    nodes: list[Node], pipes: list[Pipe]
) -> list[Issue]:
    """校验整网导入数据：重号、自环、悬空端点、无阀管段初始关闭。"""
    issues: list[Issue] = []

    node_ids: set[str] = set()
    for n in nodes:
        if n.id in node_ids:
            issues.append(Issue(DUPLICATE_NODE_ID, f"节点编号重复：{n.id}", n.id))
        node_ids.add(n.id)

    pipe_ids: set[str] = set()
    for p in pipes:
        if p.id in pipe_ids:
            issues.append(Issue(DUPLICATE_PIPE_ID, f"管段编号重复：{p.id}", p.id))
        pipe_ids.add(p.id)

        if p.from_node == p.to_node:
            issues.append(Issue(PIPE_SELF_LOOP, f"管段自环（两端为同一节点）：{p.id}", p.id))
        for endpoint in (p.from_node, p.to_node):
            if endpoint not in node_ids:
                issues.append(
                    Issue(
                        PIPE_ENDPOINT_UNKNOWN,
                        f"管段 {p.id} 的端点 {endpoint} 未在节点列表中定义（悬空端点）",
                        p.id,
                    )
                )
        if p.valve_closed and not p.has_valve:
            issues.append(
                Issue(
                    NON_VALVE_PIPE_CLOSED,
                    f"管段 {p.id} 没有阀门，不能设置为关闭状态",
                    p.id,
                )
            )
    return issues


# ---------------------------------------------------------------- 可达性


def _reachable(
    nodes: dict[str, Node],
    pipes: list[Pipe],
    removed_pipe_ids: frozenset[str],
) -> set[str]:
    """无向 BFS：从全部水源出发，返回可达节点集合。

    removed_pipe_ids 中的管段视为不存在/不可通行。
    """
    adjacency: dict[str, list[str]] = {nid: [] for nid in nodes}
    for p in pipes:
        if p.id in removed_pipe_ids:
            continue
        if p.from_node not in adjacency or p.to_node not in adjacency:
            continue
        adjacency[p.from_node].append(p.to_node)
        adjacency[p.to_node].append(p.from_node)

    reachable: set[str] = set()
    queue: deque[str] = deque()
    for nid, node in nodes.items():
        if node.type == "source":
            if nid not in reachable:
                reachable.add(nid)
                queue.append(nid)

    while queue:
        current = queue.popleft()
        for nxt in adjacency.get(current, ()):
            if nxt not in reachable:
                reachable.add(nxt)
                queue.append(nxt)
    return reachable


# ---------------------------------------------------------------- 方案评估


def validate_plan_request(
    pipes: dict[str, Pipe],
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    *,
    for_submit: bool,
) -> list[Issue]:
    """校验故障/拟关列表本身的合法性。

    for_submit=True 时额外校验“已关闭阀门不得重复关闭”。
    """
    issues: list[Issue] = []

    if fault_pipe_id not in pipes:
        issues.append(
            Issue(FAULT_PIPE_NOT_FOUND, f"故障管段不存在：{fault_pipe_id}", fault_pipe_id)
        )

    seen: set[str] = set()
    for pid in close_pipe_ids:
        if pid in seen:
            issues.append(Issue(CLOSE_PIPE_DUPLICATE, f"拟关闭管段重复：{pid}", pid))
            continue
        seen.add(pid)

        if pid == fault_pipe_id:
            issues.append(
                Issue(CLOSE_PIPE_IS_FAULT, "不能把故障管段本身作为关闭阀门", pid)
            )
            continue
        pipe = pipes.get(pid)
        if pipe is None:
            issues.append(Issue(CLOSE_PIPE_NOT_FOUND, f"拟关闭管段不存在：{pid}", pid))
        elif not pipe.has_valve:
            issues.append(Issue(CLOSE_PIPE_NO_VALVE, f"管段没有阀门，无法关闭：{pid}", pid))
        elif for_submit and pipe.valve_closed:
            issues.append(
                Issue(
                    CLOSE_PIPE_DUPLICATE,
                    f"阀门已处于关闭状态，不能重复关闭：{pid}",
                    pid,
                )
            )
    return issues


def evaluate_plan(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    *,
    for_submit: bool = False,
    pending_fault_pipe_ids: frozenset[str] | set[str] | tuple[str, ...] = (),
) -> PlanResult:
    """评估隔离方案。

    预演(for_submit=False)：只做结构校验，已关闭阀门不会产生“重复关闭”问题；
    提交(for_submit=True)：对已关闭阀门再次关闭视为 CLOSE_PIPE_DUPLICATE。

    pending_fault_pipe_ids：其他**待修复**事件的故障管段，评估时始终移除
    （故障未修复，物理上不通水），隔离预演与提交都计入这些故障。
    """
    issues = validate_plan_request(
        pipes, fault_pipe_id, close_pipe_ids, for_submit=for_submit
    )
    blocking = [i for i in issues if i.code in BLOCKING_ISSUE_CODES]

    fault = pipes.get(fault_pipe_id)
    if fault is None or blocking:
        # 结构不合法：无法进行可达性评估
        return PlanResult(
            isolatable=False, affected_sink_ids=[], issues=issues
        )

    pipe_list = list(pipes.values())

    # 方案前（当前态）：已关闭阀门不可通行；其他待修复故障管段同样移除
    already_closed = frozenset(p.id for p in pipe_list if p.has_valve and p.valve_closed)
    other_faults = frozenset(pending_fault_pipe_ids)
    before_removed = already_closed | other_faults
    before_reachable = _reachable(nodes, pipe_list, before_removed)

    # 方案后：本事件故障管段移除；其他待修复故障管段继续移除；
    # 已关阀门继续生效；拟关阀门全部关闭
    blocked = already_closed | frozenset(close_pipe_ids)
    after_removed = blocked | other_faults | frozenset({fault_pipe_id})
    after_reachable = _reachable(nodes, pipe_list, after_removed)

    both_ends_isolated = (
        fault.from_node not in after_reachable
        and fault.to_node not in after_reachable
    )

    affected = sorted(
        nid
        for nid, node in nodes.items()
        if node.type == "sink" and nid in before_reachable and nid not in after_reachable
    )

    return PlanResult(
        isolatable=both_ends_isolated,
        affected_sink_ids=affected,
        issues=issues,
        blocked_pipe_ids=frozenset(blocked),
    )


# ---------------------------------------------------------------- 复供评估


def evaluate_restoration(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    event: IsolationEvent,
    pending_events: list[IsolationEvent],
) -> RestorationResult:
    """评估某条待修复事件的复供方案（只读，不改状态）。

    pending_events 须包含全部待修复事件（含 event 自身）。
    计算通水时：
    - 排除其他待修复事件的故障管段（故障仍在）；
    - 排除仍关闭的阀门，但本事件关闭的阀门按重开计。

    安全性：仅当重开后其余待修复故障管段**两端**仍均无法从水源到达，
    才 safe=true；否则给出 RESUPPLY_RISK 阻断原因（逐条故障管段、逐端列出）。
    """
    pipe_list = list(pipes.values())
    reopen = frozenset(event.closed_pipe_ids)

    other_faults = frozenset(
        e.fault_pipe_id
        for e in pending_events
        if e.id != event.id and e.status == "pending"
    )

    # 当前态（方案前）：所有已关阀门（含本事件阀门）+ 其他待修复故障管段
    closed_before = frozenset(
        p.id for p in pipe_list if p.has_valve and p.valve_closed
    )
    before_removed = closed_before | other_faults
    before_reachable = _reachable(nodes, pipe_list, before_removed)

    # 复供后：本事件阀门重开；其余阀门若仍关闭则继续阻断；其他故障管段继续移除
    closed_after = closed_before - reopen
    after_removed = closed_after | other_faults
    after_reachable = _reachable(nodes, pipe_list, after_removed)

    restored = sorted(
        nid
        for nid, node in nodes.items()
        if node.type == "sink" and nid not in before_reachable and nid in after_reachable
    )

    issues: list[Issue] = []
    for e in pending_events:
        if e.id == event.id or e.status != "pending":
            continue
        fault = pipes.get(e.fault_pipe_id)
        if fault is None:
            # 整网替换后事件已清空；若出现游离编号，保守视为两端进水风险
            issues.append(
                Issue(
                    RESUPPLY_RISK,
                    f"重开阀门后待修复事件 {e.id} 的故障管段 {e.fault_pipe_id} 不存在，无法确认安全",
                    e.fault_pipe_id,
                )
            )
            continue
        reachable_ends = [
            end for end in (fault.from_node, fault.to_node) if end in after_reachable
        ]
        if reachable_ends:
            issues.append(
                Issue(
                    RESUPPLY_RISK,
                    f"重开阀门后，待修复事件 {e.id} 的故障管段 {fault.id} 的端点 "
                    f"{'、'.join(reachable_ends)} 将重新从水源进水，存在复供风险",
                    fault.id,
                )
            )

    return RestorationResult(
        safe=not issues,
        restored_sink_ids=restored,
        issues=issues,
        blocked_pipe_ids=frozenset(after_removed),
    )
