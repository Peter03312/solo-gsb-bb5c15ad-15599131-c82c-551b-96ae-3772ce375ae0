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
    RESUPPLY_RISK,
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

    def test_other_pending_fault_pipes_are_removed_in_evaluation(self):
        # 待修复事件的故障管段在隔离预演/提交计算中按移除处理：
        # 若另一待修复故障管段是某用水点唯一通道，该点方案前即失供，不计本次。
        nodes, pipes = build_demo()
        # P3(A-U1) 是另一待修复故障：U1 在方案前就不可达
        result = evaluate_plan(
            nodes, pipes, "P6", ["P4"], pending_fault_pipe_ids=frozenset({"P3"})
        )
        self.assertTrue(result.isolatable)
        self.assertEqual(result.affected_sink_ids, ["U3"])

    def test_pending_fault_sink_excluded_from_affected(self):
        # 待修复故障 P6(D-U3) 在方案前已移除 -> U3 方案前即失供，不计本次；
        # 本次故障 P3(A-U1)、无拟关阀门（A 端恒可达，不可隔离），仅 U1 新失供。
        nodes, pipes = build_demo()
        result = evaluate_plan(
            nodes, pipes, "P3", [], pending_fault_pipe_ids=frozenset({"P6"})
        )
        self.assertFalse(result.isolatable)
        self.assertEqual(result.affected_sink_ids, ["U1"])


class RestorationTests(unittest.TestCase):
    def _pending(self, event_id, fault, closed):
        return IsolationEvent(
            id=event_id, fault_pipe_id=fault, closed_pipe_ids=tuple(closed)
        )

    def test_restore_single_event_safe_restores_sink(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        event = self._pending(1, "P6", ["P4"])
        result = evaluate_restoration(nodes, pipes, event, [event])
        self.assertTrue(result.safe)
        self.assertEqual(result.restored_sink_ids, ["U3"])
        self.assertEqual(result.issues, [])
        # 复供后无阻断管段（P4 已重开）
        self.assertEqual(result.blocked_pipe_ids, frozenset())

    def test_restore_preview_does_not_mutate(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        event = self._pending(1, "P6", ["P4"])
        evaluate_restoration(nodes, pipes, event, [event])
        self.assertTrue(pipes["P4"].valve_closed)

    def test_unsafe_when_reopen_reaches_other_pending_fault_end(self):
        # 环形网：E1 关 P1/P7 隔离 P2；E2 在 P1/P7 已关前提下仅关 P4 隔离 P6。
        nodes = {
            "S": Node("S", "source"),
            "A": Node("A", "junction"),
            "B": Node("B", "junction"),
            "D": Node("D", "junction"),
            "U1": Node("U1", "sink"),
            "U2": Node("U2", "sink"),
        }
        pipes = {
            "P1": Pipe("P1", "S", "A", has_valve=True, valve_closed=True),
            "P2": Pipe("P2", "A", "B", has_valve=True),
            "P3": Pipe("P3", "B", "U1", has_valve=True),
            "P4": Pipe("P4", "B", "D", has_valve=True, valve_closed=True),
            "P6": Pipe("P6", "D", "U2", has_valve=False),
            "P7": Pipe("P7", "A", "D", has_valve=True, valve_closed=True),
        }
        e1 = self._pending(1, "P2", ["P1", "P7"])
        e2 = self._pending(2, "P6", ["P4"])

        # 先复供 E1：P7 重开后水直达 D（P6 的 D 端）-> 不安全
        result = evaluate_restoration(nodes, pipes, e1, [e1, e2])
        self.assertFalse(result.safe)
        self.assertTrue(
            any(i.code == RESUPPLY_RISK and i.pipe_id == "P6" for i in result.issues)
        )
        # U2 在 E2 提交后（P4 关闭）已失供，复供方案前就不可达，
        # 因此本次“预计恢复”不含 U2；U1 随 B 区重新进水而恢复。
        self.assertEqual(result.restored_sink_ids, ["U1"])

        # 先复供 E2：只重开 P4，P1/P7 仍关 -> 安全，无新增恢复点
        result = evaluate_restoration(nodes, pipes, e2, [e1, e2])
        self.assertTrue(result.safe)
        self.assertEqual(result.issues, [])
        self.assertEqual(result.restored_sink_ids, [])
        # E2 复供后 P1/P7 仍关，其他待修复故障 P2 仍移除
        self.assertEqual(result.blocked_pipe_ids, frozenset({"P1", "P7", "P2"}))

    def test_other_restored_events_are_ignored(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        event = self._pending(1, "P6", ["P4"])
        old = IsolationEvent(id=9, fault_pipe_id="P3", closed_pipe_ids=(),
                             status="restored")
        result = evaluate_restoration(nodes, pipes, event, [event, old])
        self.assertTrue(result.safe)

    def test_unrelated_still_closed_valves_keep_blocking(self):
        # P3 阀仍由别的待修复事件关闭：复供 E1 重开 P4 后 U1 依旧失供
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        pipes["P3"] = Pipe("P3", "A", "U1", has_valve=True, valve_closed=True)
        e1 = self._pending(1, "P6", ["P4"])
        # 另一待修复事件故障 P5（B-U2）：复供 E1 后 B 侧仍可达，其隔离靠 P3 关阀
        other = self._pending(2, "P5", ["P3"])
        result = evaluate_restoration(nodes, pipes, e1, [e1, other])
        # P5 的 B 端重开 P4 后可达 -> 该事件本身会被标记进水风险，
        # 本用例只验证“仍关闭阀门继续阻断”：U1 不进入恢复列表、P3 在阻断集中
        self.assertEqual(result.restored_sink_ids, ["U3"])
        self.assertIn("P3", result.blocked_pipe_ids)


if __name__ == "__main__":
    unittest.main()
