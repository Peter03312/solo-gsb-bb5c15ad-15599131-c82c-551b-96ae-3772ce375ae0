"""故障隔离核心逻辑的单元测试（仅依赖标准库）。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.planning import (  # noqa: E402
    Node,
    Pipe,
    IsolationEvent,
    evaluate_plan,
    evaluate_restoration,
    validate_import,
    DUPLICATE_NODE_ID,
    DUPLICATE_PIPE_ID,
    PIPE_SELF_LOOP,
    PIPE_ENDPOINT_UNKNOWN,
    NON_VALVE_PIPE_CLOSED,
    FAULT_PIPE_NOT_FOUND,
    CLOSE_PIPE_NOT_FOUND,
    CLOSE_PIPE_NO_VALVE,
    CLOSE_PIPE_DUPLICATE,
    CLOSE_PIPE_IS_FAULT,
    RESTORE_REWATER_RISK,
    BLOCKED_BY_PENDING_FAULT,
    BLOCKED_BY_CLOSED_VALVE,
)


def build_demo():
    """演示管网：

    S --P1(v)-- A --P2(v)-- B --P4(v)-- D --P6(无阀)-- U3
                 |           |
               P3(v)       P5(v)
                 |           |
                 U1          U2
    故障管段 P6（B? 不：D-U3）。
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "D": Node("D", "junction"),
        "U1": Node("U1", "sink"),
        "U2": Node("U2", "sink"),
        "U3": Node("U3", "sink"),
    }
    pipes = {
        "P1": Pipe("P1", "S", "A", has_valve=True),
        "P2": Pipe("P2", "A", "B", has_valve=True),
        "P3": Pipe("P3", "A", "U1", has_valve=True),
        "P4": Pipe("P4", "B", "D", has_valve=True),
        "P5": Pipe("P5", "B", "U2", has_valve=True),
        "P6": Pipe("P6", "D", "U3", has_valve=False),
    }
    return nodes, pipes


class ImportValidationTests(unittest.TestCase):
    def test_valid_import(self):
        nodes, pipes = build_demo()
        self.assertEqual(validate_import(list(nodes.values()), list(pipes.values())), [])

    def test_dangling_endpoint(self):
        nodes = [Node("S", "source")]
        pipes = [Pipe("P1", "S", "X")]
        issues = validate_import(nodes, pipes)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, PIPE_ENDPOINT_UNKNOWN)

    def test_self_loop(self):
        nodes = [Node("A", "junction")]
        pipes = [Pipe("P1", "A", "A")]
        issues = validate_import(nodes, pipes)
        self.assertTrue(any(i.code == PIPE_SELF_LOOP for i in issues))

    def test_duplicate_ids(self):
        nodes = [Node("A", "junction"), Node("A", "sink")]
        pipes = [
            Pipe("P1", "A", "A"),  # 自环也会报
            Pipe("P1", "A", "A"),
        ]
        issues = validate_import(nodes, pipes)
        codes = [i.code for i in issues]
        self.assertIn(DUPLICATE_NODE_ID, codes)
        self.assertIn(DUPLICATE_PIPE_ID, codes)

    def test_non_valve_closed_rejected(self):
        nodes = [Node("S", "source"), Node("U", "sink")]
        pipes = [Pipe("P1", "S", "U", has_valve=False, valve_closed=True)]
        issues = validate_import(nodes, pipes)
        self.assertTrue(any(i.code == NON_VALVE_PIPE_CLOSED for i in issues))


class IsolationPlanTests(unittest.TestCase):
    def test_isolatable_plan_with_one_closed_valve(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["P4"])
        self.assertTrue(result.isolatable)
        self.assertEqual(result.affected_sink_ids, ["U3"])
        self.assertEqual(result.issues, [])

    def test_non_isolatable_plan(self):
        nodes, pipes = build_demo()
        # 只关 P5（B-U2）：D 仍可经 P4 从水源到达，故障管段隔离失败
        result = evaluate_plan(nodes, pipes, "P6", ["P5"])
        self.assertFalse(result.isolatable)
        # U2 因关阀失供，U3 因故障管段移除而失供，均按“前可达后不可达”统计
        self.assertEqual(result.affected_sink_ids, ["U2", "U3"])

    def test_already_closed_valve_keeps_effect_preview(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        # 预演时无需重复关闭，已关阀门继续生效
        result = evaluate_plan(nodes, pipes, "P6", [])
        self.assertTrue(result.isolatable)
        # P4 此前已关闭，U3 在方案前就已失供，不计入本次新增失供用水点
        self.assertEqual(result.affected_sink_ids, [])

    def test_submit_rejects_closing_already_closed_valve(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        result = evaluate_plan(nodes, pipes, "P6", ["P4"], for_submit=True)
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_DUPLICATE for i in result.issues))

    def test_fault_pipe_not_found(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "PX", ["P4"])
        self.assertFalse(result.isolatable)
        self.assertEqual(result.issues[0].code, FAULT_PIPE_NOT_FOUND)

    def test_close_pipe_not_found(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["PX"])
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_NOT_FOUND for i in result.issues))

    def test_close_pipe_without_valve(self):
        nodes, pipes = build_demo()
        # P6 是故障管段，单独构造一个无阀且非故障的管段来测试
        nodes["E"] = Node("E", "junction")
        pipes["P7"] = Pipe("P7", "B", "E", has_valve=False)
        result = evaluate_plan(nodes, pipes, "P6", ["P7"])
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_NO_VALVE for i in result.issues))

    def test_close_fault_pipe_itself_rejected(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["P6"])
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_IS_FAULT for i in result.issues))

    def test_duplicate_close_entries(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["P4", "P4"])
        self.assertFalse(result.isolatable)
        dupes = [i for i in result.issues if i.code == CLOSE_PIPE_DUPLICATE]
        self.assertEqual(len(dupes), 1)

    def test_source_endpoint_always_reachable(self):
        # 故障管段直连水源且无额外阀门：水源端恒可达，不能隔离
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P1", [])
        self.assertFalse(result.isolatable)

    def test_affected_sinks_sorted(self):
        nodes, pipes = build_demo()
        # 关 P3 后移除 P6：U1、U3 失供（U1 字典序在前）
        result = evaluate_plan(nodes, pipes, "P6", ["P3"])
        self.assertEqual(result.affected_sink_ids, ["U1", "U3"])
        self.assertFalse(result.isolatable)

    def test_no_affected_sink_when_fault_is_on_dead_branch_without_sink(self):
        nodes = {
            "S": Node("S", "source"),
            "A": Node("A", "junction"),
            "B": Node("B", "junction"),
            "U1": Node("U1", "sink"),
        }
        pipes = {
            "P1": Pipe("P1", "S", "A", has_valve=True),
            "P2": Pipe("P2", "A", "B", has_valve=True),
            "P3": Pipe("P3", "A", "U1", has_valve=True),
        }
        # 故障 P2（A-B，B 为盲端连接点），关 P2 自身不可（是故障）；
        # 改为故障 P2、无阀门可关 -> 不可隔离；失供用水点为空
        result = evaluate_plan(nodes, pipes, "P2", [])
        self.assertFalse(result.isolatable)
        self.assertEqual(result.affected_sink_ids, [])


def build_nested():
    """双故障复供场景：

    S--P1--A--P2--B--P4(v)--D--P6(故障)->U3
                       |--P5(v)--U2     |--P7(v)--E--P8(故障)->U4
    A--P3(v)--U1
    返回 (nodes, pipes)，P4 已置为关闭（E1 提交后的阀位）。
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "D": Node("D", "junction"),
        "E": Node("E", "junction"),
        "U1": Node("U1", "sink"),
        "U2": Node("U2", "sink"),
        "U3": Node("U3", "sink"),
        "U4": Node("U4", "sink"),
    }
    pipes = {
        "P1": Pipe("P1", "S", "A", has_valve=True),
        "P2": Pipe("P2", "A", "B", has_valve=True),
        "P3": Pipe("P3", "A", "U1", has_valve=True),
        "P4": Pipe("P4", "B", "D", has_valve=True, valve_closed=True),
        "P5": Pipe("P5", "B", "U2", has_valve=True),
        "P6": Pipe("P6", "D", "U3", has_valve=False),
        "P7": Pipe("P7", "D", "E", has_valve=True),
        "P8": Pipe("P8", "E", "U4", has_valve=False),
    }
    return nodes, pipes


class PendingFaultPlanTests(unittest.TestCase):
    def _e1(self):
        # E1：关 P4 隔离 P6，U3/U4 失供
        return IsolationEvent(
            id=1,
            fault_pipe_id="P6",
            closed_pipe_ids=("P4",),
            affected_sink_ids=("U3", "U4"),
            version_before=1,
            version_after=2,
        )

    def test_preview_inside_pending_isolation_zone(self):
        nodes, pipes = build_nested()
        e1 = self._e1()
        # P8 在 E1 隔离区内：P4 已断水，无需关阀即可隔离；U4 方案前已失供，不重复计入
        result = evaluate_plan(nodes, pipes, "P8", [], pending_events=[e1])
        self.assertTrue(result.isolatable)
        self.assertEqual(result.affected_sink_ids, [])

    def test_pending_events_drive_exclusion(self):
        # 阀位真值尚未关闭 P4，仅靠 pending 事件表达隔离状态：
        # 待修复事件的故障管段与关闭阀都必须计入方案前/方案后图。
        nodes, pipes = build_nested()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=False)
        e1 = self._e1()

        # 不计入待修复事件：P4 通畅，P8 无法隔离（E 端仍可达），U4 方案前可达
        result = evaluate_plan(nodes, pipes, "P8", [], pending_events=[])
        self.assertFalse(result.isolatable)
        self.assertEqual(result.affected_sink_ids, ["U4"])

        # 计入待修复事件 E1：P4 被其占用关闭、P6 移除，P8 两端均脱离水源，
        # 且 U4 方案前已失供，不重复计入
        result = evaluate_plan(nodes, pipes, "P8", [], pending_events=[e1])
        self.assertTrue(result.isolatable)
        self.assertEqual(result.affected_sink_ids, [])

    def test_restored_events_not_counted(self):
        nodes, pipes = build_nested()
        e1 = IsolationEvent(
            id=1,
            fault_pipe_id="P6",
            closed_pipe_ids=("P4",),
            affected_sink_ids=("U3", "U4"),
            status="restored",
        )
        # 已复供事件的阀位真值仍是关闭（测试数据），但其故障管段不额外移除；
        # 结构上 P6 不移除不影响 P8 隔离判定
        result = evaluate_plan(nodes, pipes, "P8", [], pending_events=[e1])
        self.assertTrue(result.isolatable)


class RestorationTests(unittest.TestCase):
    def _events(self):
        nodes, pipes = build_nested()
        e1 = IsolationEvent(
            id=1,
            fault_pipe_id="P6",
            closed_pipe_ids=("P4",),
            affected_sink_ids=("U3", "U4"),
            version_before=1,
            version_after=2,
        )
        e2 = IsolationEvent(
            id=2,
            fault_pipe_id="P8",
            closed_pipe_ids=(),
            affected_sink_ids=(),
            version_before=2,
            version_after=3,
        )
        return nodes, pipes, e1, e2

    def test_restore_safe_without_other_pending(self):
        nodes, pipes, e1, _ = self._events()
        result = evaluate_restoration(nodes, pipes, e1, [e1])
        self.assertTrue(result.safe)
        self.assertEqual(result.reopened_pipe_ids, ["P4"])
        self.assertEqual(result.recoverable_sink_ids, ["U3", "U4"])
        self.assertEqual(result.still_blocked_sink_ids, [])
        self.assertEqual(result.issues, [])

    def test_restore_unsafe_rewater_risk(self):
        nodes, pipes, e1, e2 = self._events()
        result = evaluate_restoration(nodes, pipes, e1, [e1, e2])
        self.assertFalse(result.safe)
        risk = [i for i in result.issues if i.code == RESTORE_REWATER_RISK]
        self.assertEqual(len(risk), 1)
        self.assertEqual(risk[0].event_id, 2)
        self.assertEqual(risk[0].pipe_id, "P8")
        # U3 可恢复，U4 被待修复故障 P8 继续阻断
        self.assertEqual(result.recoverable_sink_ids, ["U3"])
        self.assertEqual(result.still_blocked_sink_ids, ["U4"])
        block = next(
            i for i in result.issues if i.code == BLOCKED_BY_PENDING_FAULT
        )
        self.assertEqual(block.event_id, 2)
        self.assertEqual(block.related_pipe_id, "P8")

    def test_restore_inner_event_first_is_safe(self):
        nodes, pipes, e1, e2 = self._events()
        result = evaluate_restoration(nodes, pipes, e2, [e1, e2])
        self.assertTrue(result.safe)
        # E2 没有关阀；P4 仍由 E1 占用；P8 仍被 P4 隔离，无用水点恢复
        self.assertEqual(result.reopened_pipe_ids, [])
        self.assertEqual(result.recoverable_sink_ids, [])
        self.assertEqual(result.remaining_closed_pipe_ids, ["P4"])

    def test_shared_valve_kept_closed_for_other_event(self):
        # 两个事件共用一个阀门：复供先发生的事件时阀门不能重开
        nodes = {
            "S": Node("S", "source"),
            "A": Node("A", "junction"),
            "B": Node("B", "junction"),
            "U1": Node("U1", "sink"),
            "U2": Node("U2", "sink"),
        }
        pipes = {
            "P1": Pipe("P1", "S", "A", has_valve=True, valve_closed=True),
            "P2": Pipe("P2", "A", "U1", has_valve=False),
            "P3": Pipe("P3", "A", "B", has_valve=False),
            "P4": Pipe("P4", "B", "U2", has_valve=False),
        }
        e1 = IsolationEvent(
            1, "P2", ("P1",), affected_sink_ids=("U1", "U2"),
            version_before=1, version_after=2,
        )
        e2 = IsolationEvent(
            2, "P4", ("P1",), affected_sink_ids=(),
            version_before=2, version_after=3,
        )
        # 复供 E1：P1 仍被 E2 占用，不能重开；P4 两端仍不可达 -> 安全但 U1/U2 都不恢复
        result = evaluate_restoration(nodes, pipes, e1, [e1, e2])
        self.assertTrue(result.safe)
        self.assertEqual(result.reopened_pipe_ids, [])
        self.assertEqual(result.recoverable_sink_ids, [])
        self.assertEqual(set(result.remaining_closed_pipe_ids), {"P1"})
        blocked = {i.code for i in result.issues}
        # U1 的最近阻断物是仍关闭的 P1；U2 的最短路径先碰到待修复故障管段 P4
        self.assertEqual(
            blocked, {BLOCKED_BY_CLOSED_VALVE, BLOCKED_BY_PENDING_FAULT}
        )
        # E2 也复供后，再复供 E1（单独）：P1 可重开
        pipes_after = {
            "P1": Pipe("P1", "S", "A", has_valve=True, valve_closed=True),
            "P2": Pipe("P2", "A", "U1", has_valve=False),
            "P3": Pipe("P3", "A", "B", has_valve=False),
            "P4": Pipe("P4", "B", "U2", has_valve=False),
        }
        result = evaluate_restoration(nodes, pipes_after, e1, [e1])
        self.assertTrue(result.safe)
        self.assertEqual(result.reopened_pipe_ids, ["P1"])
        self.assertEqual(result.recoverable_sink_ids, ["U1", "U2"])


if __name__ == "__main__":
    unittest.main()
