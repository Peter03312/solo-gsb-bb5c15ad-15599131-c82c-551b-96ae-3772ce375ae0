"""API 路由层集成测试。

本机无 PostgreSQL，使用内存假连接替身替换 app.db 的数据访问函数，
覆盖 FastAPI 路由、请求校验、事务与错误码的完整行为。
运行：python3 -m unittest tests.test_api -v
（需要 fastapi/httpx 依赖）
"""
import json
import os
import sys
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.planning import Node, Pipe  # noqa: E402


class FakeStore:
    def __init__(self):
        self.version = 0
        self.updated_at = None
        self.nodes: dict[str, Node] = {}
        self.pipes: dict[str, Pipe] = {}
        self.events = []
        self.next_event_id = 1
        self.fail_commit = False


STORE = FakeStore()


@contextmanager
def fake_get_conn():
    yield "FAKE_CONN"


@contextmanager
def fake_transaction():
    conn = "FAKE_TX"
    # 简单快照：提交时把暂存区落盘
    snapshot = (
        STORE.version,
        dict(STORE.nodes),
        {k: Pipe(**v.__dict__) for k, v in STORE.pipes.items()},
    )
    tmp = {
        "version": STORE.version,
        "nodes": dict(STORE.nodes),
        "pipes": dict(STORE.pipes),
        "events": list(STORE.events),
        "next_id": STORE.next_event_id,
    }
    try:
        yield conn
        if STORE.fail_commit:
            raise RuntimeError("simulated commit failure")
    except Exception:
        STORE.version = tmp["version"]
        STORE.nodes = tmp["nodes"]
        STORE.pipes = tmp["pipes"]
        STORE.events = tmp["events"]
        STORE.next_event_id = tmp["next_id"]
        raise


def fake_init_db():
    return None


def fake_load_snapshot(conn, for_update=False):
    return STORE.version, STORE.updated_at, dict(STORE.nodes), dict(STORE.pipes)


def fake_replace_network(conn, nodes, pipes):
    STORE.version = 1
    STORE.nodes = {n.id: n for n in nodes}
    STORE.pipes = {p.id: p for p in pipes}
    STORE.events = []
    STORE.next_event_id = 1
    return 1


def fake_submit_isolation(conn, expected_version, fault_pipe_id, close_pipe_ids, result):
    # 复制 db.submit_isolation 的关键语义：锁行版本比对 -> 更新阀位 -> +1 -> 事件
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    for pid in close_pipe_ids:
        p = STORE.pipes[pid]
        STORE.pipes[pid] = Pipe(
            p.id, p.from_node, p.to_node, p.has_valve, True, p.name
        )
    before = STORE.version
    STORE.version += 1
    eid = STORE.next_event_id
    STORE.next_event_id += 1
    import datetime

    event = {
        "id": eid,
        "version_before": before,
        "version_after": STORE.version,
        "fault_pipe_id": fault_pipe_id,
        "closed_pipe_ids": list(close_pipe_ids),
        "affected_sink_ids": list(result.affected_sink_ids),
        "status": "pending",
        "restored_at": None,
        "restored_version": None,
        "reopened_pipe_ids": [],
        "created_at": datetime.datetime(2026, 9, 30, 12, 0, 0),
    }
    STORE.events.insert(0, event)
    return {"version": STORE.version, "event_id": eid, "created_at": event["created_at"]}


def fake_list_events(conn, limit):
    return STORE.events[:limit]


def fake_load_event(conn, event_id, for_update=False):
    for e in STORE.events:
        if e["id"] == event_id:
            return dict(e)
    return None


def fake_load_pending_events(conn):
    return [dict(e) for e in STORE.events if e["status"] == "pending"]


def fake_confirm_restoration(
    conn, expected_version, event_id, reopened_pipe_ids, recoverable_sink_ids
):
    import datetime

    # 复制 db.confirm_restoration 的关键语义：锁版本 -> 锁事件 -> 重开阀 -> +1 -> 置已复供
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    event = next((e for e in STORE.events if e["id"] == event_id), None)
    if event is None:
        raise db.EventNotFound(event_id)
    if event["status"] == "restored":
        raise db.EventAlreadyRestored(event_id)

    occupied = set()
    for e in STORE.events:
        if e["status"] == "pending" and e["id"] != event_id:
            occupied.update(e["closed_pipe_ids"])
    actual_reopen = [pid for pid in reopened_pipe_ids if pid not in occupied]
    for pid in actual_reopen:
        p = STORE.pipes[pid]
        STORE.pipes[pid] = Pipe(
            p.id, p.from_node, p.to_node, p.has_valve, False, p.name
        )

    STORE.version += 1
    event["status"] = "restored"
    event["restored_at"] = datetime.datetime(2026, 9, 30, 13, 0, 0)
    event["restored_version"] = STORE.version
    event["reopened_pipe_ids"] = list(actual_reopen)
    return {
        "version": STORE.version,
        "event_id": event_id,
        "reopened_pipe_ids": list(actual_reopen),
        "recoverable_sink_ids": list(recoverable_sink_ids),
    }


# 替换数据访问层
db.get_conn = fake_get_conn
db.transaction = fake_transaction
db.init_db = fake_init_db
db.load_snapshot = fake_load_snapshot
db.replace_network = fake_replace_network
db.submit_isolation = fake_submit_isolation
db.list_events = fake_list_events
db.load_event = fake_load_event
db.load_pending_events = fake_load_pending_events
db.confirm_restoration = fake_confirm_restoration


VALID_NETWORK = {
    "nodes": [
        {"id": "S", "type": "source", "name": "一水厂"},
        {"id": "A", "type": "junction"},
        {"id": "B", "type": "junction"},
        {"id": "D", "type": "junction"},
        {"id": "U1", "type": "sink"},
        {"id": "U2", "type": "sink"},
        {"id": "U3", "type": "sink"},
    ],
    "pipes": [
        {"id": "P1", "from": "S", "to": "A", "has_valve": True},
        {"id": "P2", "from": "A", "to": "B", "has_valve": True},
        {"id": "P3", "from": "A", "to": "U1", "has_valve": True},
        {"id": "P4", "from": "B", "to": "D", "has_valve": True},
        {"id": "P5", "from": "B", "to": "U2", "has_valve": True},
        {"id": "P6", "from": "D", "to": "U3", "has_valve": False},
    ],
}

# 双故障复供场景：
# S--P1--A--P2--B--P4(v)--D--P6(故障)->U3
#                    |--P5(v)--U2      |--P7(v)--E--P8(故障)->U4
# A--P3(v)--U1
NESTED_NETWORK = {
    "nodes": [
        {"id": "S", "type": "source"},
        {"id": "A", "type": "junction"},
        {"id": "B", "type": "junction"},
        {"id": "D", "type": "junction"},
        {"id": "E", "type": "junction"},
        {"id": "U1", "type": "sink"},
        {"id": "U2", "type": "sink"},
        {"id": "U3", "type": "sink"},
        {"id": "U4", "type": "sink"},
    ],
    "pipes": [
        {"id": "P1", "from": "S", "to": "A", "has_valve": True},
        {"id": "P2", "from": "A", "to": "B", "has_valve": True},
        {"id": "P3", "from": "A", "to": "U1", "has_valve": True},
        {"id": "P4", "from": "B", "to": "D", "has_valve": True},
        {"id": "P5", "from": "B", "to": "U2", "has_valve": True},
        {"id": "P6", "from": "D", "to": "U3", "has_valve": False},
        {"id": "P7", "from": "D", "to": "E", "has_valve": True},
        {"id": "P8", "from": "E", "to": "U4", "has_valve": False},
    ],
}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # lifespan 会调用 init_db（已替换为 fake）
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.fail_commit = False

    def _import(self):
        r = self.client.post("/api/network/import", json=VALID_NETWORK)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_full_happy_path(self):
        info = self._import()
        self.assertEqual(info["version"], 1)

        r = self.client.get("/api/network")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(len(body["nodes"]), 7)
        self.assertEqual(len(body["pipes"]), 6)
        # 序列化别名：from/to
        p1 = next(p for p in body["pipes"] if p["id"] == "P1")
        self.assertEqual(p1["from"], "S")
        self.assertEqual(p1["to"], "A")
        # 无阀管段 valve_closed 序列化为 null
        p6 = next(p for p in body["pipes"] if p["id"] == "P6")
        self.assertIsNone(p6["valve_closed"])

        # 不可行预演
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P5"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["isolatable"])

        # 可行预演
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        body = r.json()
        self.assertTrue(body["isolatable"])
        self.assertEqual(body["affected_sink_ids"], ["U3"])
        self.assertTrue(body["version_match"])

        # 提交成功
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["affected_sink_ids"], ["U3"])
        self.assertEqual(body["closed_pipe_ids"], ["P4"])
        self.assertEqual(body["event_id"], 1)

        # 阀位确实落库
        r = self.client.get("/api/network")
        p4 = next(p for p in r.json()["pipes"] if p["id"] == "P4")
        self.assertTrue(p4["valve_closed"])

        # 历史
        r = self.client.get("/api/isolation/events")
        self.assertEqual(r.status_code, 200)
        events = r.json()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["fault_pipe_id"], "P6")
        self.assertEqual(events[0]["closed_pipe_ids"], ["P4"])

    def test_import_rejects_dangling_self_loop_duplicate(self):
        bad = {
            "nodes": [
                {"id": "S", "type": "source"},
                {"id": "S", "type": "junction"},
            ],
            "pipes": [
                {"id": "P1", "from": "S", "to": "X"},
                {"id": "P1", "from": "S", "to": "S"},
            ],
        }
        r = self.client.post("/api/network/import", json=bad)
        self.assertEqual(r.status_code, 422)
        codes = {i["code"] for i in r.json()["error"]["issues"]}
        self.assertIn("DUPLICATE_NODE_ID", codes)
        self.assertIn("DUPLICATE_PIPE_ID", codes)
        self.assertIn("PIPE_ENDPOINT_UNKNOWN", codes)
        self.assertIn("PIPE_SELF_LOOP", codes)
        # 拒绝后状态不变（空网版本 0）
        r = self.client.get("/api/network")
        self.assertEqual(r.json()["version"], 0)

    def test_non_valve_pipe_initial_closed_rejected(self):
        bad = dict(VALID_NETWORK)
        bad["pipes"] = [
            dict(p, valve_closed=True) if p["id"] == "P6" else p
            for p in VALID_NETWORK["pipes"]
        ]
        r = self.client.post("/api/network/import", json=bad)
        self.assertEqual(r.status_code, 422)
        self.assertIn("NON_VALVE_PIPE_CLOSED", r.text)

    def test_submit_version_conflict(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 99},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        # 状态不变
        self.assertEqual(STORE.version, 1)

    def test_submit_no_valve_pipe(self):
        self._import()
        # 关闭无阀管段 P6（以 P5 为故障管段）-> 422 CLOSE_PIPE_NO_VALVE
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P5", "close_pipe_ids": ["P6"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_NO_VALVE", r.text)
        self.assertEqual(STORE.version, 1)

    def test_submit_duplicate_and_already_closed(self):
        self._import()
        # 列表内重复
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4", "P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_DUPLICATE", r.text)

        # 先成功关 P4
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)

        # 版本 2 下再次关已关闭的 P4
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 2},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_DUPLICATE", r.text)

    def test_submit_not_isolatable(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P5"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "NOT_ISOLATABLE")
        self.assertEqual(STORE.version, 1)

    def test_submit_fault_pipe_as_valve(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P6"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_IS_FAULT", r.text)

    def test_preview_stale_version_flag(self):
        self._import()
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 5},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["version_match"])
        self.assertEqual(body["version"], 1)

    def test_request_body_validation_422(self):
        self._import()
        # version 不允许负数
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": [], "version": -1},
        )
        self.assertEqual(r.status_code, 422)

    def test_submit_against_empty_network_is_version_conflict(self):
        # 空网版本为 0：基于版本 1 提交应得到 409，而非参数校验错误
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")

    def test_reimport_resets_version_and_events(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(STORE.version, 2)
        # 重新导入 -> 版本重置为 1、历史清空
        r = self.client.post("/api/network/import", json=VALID_NETWORK)
        self.assertEqual(r.json()["version"], 1)
        self.assertEqual(STORE.events, [])

    def test_failed_submit_rolls_back(self):
        self._import()
        STORE.fail_commit = True
        # TestClient 默认抛出服务端异常；真实 HTTP 下客户端会收到 500。
        # 关键断言：事务回滚后版本与阀位均不变。
        with self.assertRaises(RuntimeError):
            self.client.post(
                "/api/isolation/submit",
                json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
            )
        self.assertEqual(STORE.version, 1)
        self.assertFalse(STORE.pipes["P4"].valve_closed)


class RestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.fail_commit = False

    def _import_nested(self):
        r = self.client.post("/api/network/import", json=NESTED_NETWORK)
        self.assertEqual(r.status_code, 200, r.text)

    def _submit(self, fault, closes, version):
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": fault, "close_pipe_ids": closes, "version": version},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_restore_preview_does_not_change_state(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 2}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["safe"])
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["closed_pipe_ids"], ["P4"])
        self.assertEqual(body["reopened_pipe_ids"], ["P4"])
        self.assertEqual(body["recoverable_sink_ids"], ["U3", "U4"])
        self.assertEqual(body["still_blocked_sink_ids"], [])
        self.assertTrue(body["version_match"])
        # 只读：阀位、版本、事件状态均不变
        self.assertEqual(STORE.version, 2)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events[0]["status"], "pending")

    def test_restore_preview_stale_version_flag(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 1}
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["version_match"])
        self.assertEqual(r.json()["version"], 2)

    def test_restore_happy_path(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)

        # 确认复供：版本 2 -> 3，P4 重开，事件置为 restored，U3/U4 恢复
        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 2}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 3)
        self.assertEqual(body["status"], "restored")
        self.assertEqual(body["reopened_pipe_ids"], ["P4"])
        self.assertEqual(body["recoverable_sink_ids"], ["U3", "U4"])

        self.assertFalse(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events[0]["status"], "restored")
        self.assertEqual(STORE.events[0]["restored_version"], 3)

        # 历史中可看到状态字段
        r = self.client.get("/api/isolation/events")
        ev = next(e for e in r.json() if e["id"] == 1)
        self.assertEqual(ev["status"], "restored")
        self.assertEqual(ev["reopened_pipe_ids"], ["P4"])

    def test_restore_event_not_found(self):
        self._import_nested()
        for path in ("restore/preview", "restore/confirm"):
            r = self.client.post(
                f"/api/isolation/events/999/{path}", json={"version": 1}
            )
            self.assertEqual(r.status_code, 404, r.text)
            self.assertEqual(r.json()["error"]["code"], "EVENT_NOT_FOUND")

    def test_restore_already_restored_rejected(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)
        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 2}
        )
        self.assertEqual(r.status_code, 200)

        # 重复确认 -> 409，状态不变
        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 3}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "EVENT_ALREADY_RESTORED")

        # 对已复供事件预演同样明确拒绝
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 3}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "EVENT_ALREADY_RESTORED")

    def test_restore_version_conflict_no_partial_change(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)
        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 1}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        # 无部分变更
        self.assertEqual(STORE.version, 2)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events[0]["status"], "pending")

    def test_nested_fault_restore_order_and_rewater_risk(self):
        self._import_nested()
        # E1：关 P4 隔离 P6（U3/U4 失供）
        e1 = self._submit("P6", ["P4"], 1)
        self.assertEqual(e1["affected_sink_ids"], ["U3", "U4"])
        # E2：在 E1 隔离区内再隔离 P8（无需关阀；U4 已失供不重复计入）
        e2 = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P8", "close_pipe_ids": [], "version": 2},
        )
        self.assertEqual(e2.status_code, 200, e2.text)
        self.assertEqual(e2.json()["affected_sink_ids"], [])

        # 直接复供 E1：重开 P4 会让 E2 的故障管段 P8 重新进水 -> 422 RESTORE_UNSAFE
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 3}
        )
        body = r.json()
        self.assertFalse(body["safe"])
        self.assertEqual(body["recoverable_sink_ids"], ["U3"])
        self.assertEqual(body["still_blocked_sink_ids"], ["U4"])
        codes = {i["code"] for i in body["issues"]}
        self.assertIn("RESTORE_REWATER_RISK", codes)
        self.assertIn("BLOCKED_BY_PENDING_FAULT", codes)

        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 3}
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "RESTORE_UNSAFE")
        # 拒绝后无部分变更：P4 仍关、版本仍 3、E1 仍待修复
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.version, 3)
        self.assertEqual(STORE.events[1]["status"], "pending")

        # 先复供 E2：无阀门可重开，安全；P8 仍被 P4 隔离
        r = self.client.post(
            "/api/isolation/events/2/restore/confirm", json={"version": 3}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["reopened_pipe_ids"], [])
        self.assertTrue(STORE.pipes["P4"].valve_closed)

        # 再复供 E1：安全，P4 重开，U3/U4 恢复
        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 4}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 5)
        self.assertEqual(body["reopened_pipe_ids"], ["P4"])
        self.assertEqual(body["recoverable_sink_ids"], ["U3", "U4"])
        self.assertFalse(STORE.pipes["P4"].valve_closed)

    def test_isolation_preview_counts_pending_faults(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)
        # E1 待修复时预演 E2：P6 故障管段与 P4 关闭阀继续计入，
        # U3/U4 在方案前已不可达，不重复计入失供
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P8", "close_pipe_ids": [], "version": 2},
        )
        body = r.json()
        self.assertTrue(body["isolatable"])
        self.assertEqual(body["affected_sink_ids"], [])

    def test_reimport_invalidates_old_events(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)
        # 整网替换后旧事件失效（历史清空），再按旧编号复供 -> 404
        r = self.client.post("/api/network/import", json=NESTED_NETWORK)
        self.assertEqual(r.status_code, 200)
        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 1}
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "EVENT_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
