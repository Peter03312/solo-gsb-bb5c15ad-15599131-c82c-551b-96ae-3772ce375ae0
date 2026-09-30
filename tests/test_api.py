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
        "created_at": datetime.datetime(2026, 9, 30, 12, 0, 0),
    }
    STORE.events.insert(0, event)
    return {"version": STORE.version, "event_id": eid, "created_at": event["created_at"]}


def fake_list_events(conn, limit):
    return STORE.events[:limit]


def fake_get_event(conn, event_id, for_update=False):
    for e in STORE.events:
        if e["id"] == event_id:
            return dict(e)
    return None


def fake_list_pending_events(conn, for_update=False):
    return [dict(e) for e in STORE.events if e["status"] == "pending"]


def fake_confirm_restoration(conn, expected_version, event_id, reopen_pipe_ids,
                             restored_sink_ids):
    # 复制 db.confirm_restoration 的关键语义：版本比对 -> 事件状态 -> 开阀 -> +1
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    event = next((e for e in STORE.events if e["id"] == event_id), None)
    if event is None:
        raise db.EventNotFound(event_id)
    if event["status"] != "pending":
        raise db.EventAlreadyRestored(event_id)
    for pid in reopen_pipe_ids:
        if pid in STORE.pipes:
            p = STORE.pipes[pid]
            STORE.pipes[pid] = Pipe(
                p.id, p.from_node, p.to_node, p.has_valve, False, p.name
            )
    STORE.version += 1
    import datetime

    event["status"] = "restored"
    event["restored_at"] = datetime.datetime(2026, 9, 30, 13, 0, 0)
    return {
        "version": STORE.version,
        "event_id": event_id,
        "restored_sink_ids": list(restored_sink_ids),
        "reopened_pipe_ids": list(reopen_pipe_ids),
    }


# 替换数据访问层
db.get_conn = fake_get_conn
db.transaction = fake_transaction
db.init_db = fake_init_db
db.load_snapshot = fake_load_snapshot
db.replace_network = fake_replace_network
db.submit_isolation = fake_submit_isolation
db.list_events = fake_list_events
db.get_event = fake_get_event
db.list_pending_events = fake_list_pending_events
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


# 环形管网：S-P1-A；A-P2-B（可作故障1）；B-P3-U1；B-P4-D；
# A-P7-D 跨接；D-P6-U2（无阀，可作故障2）。
# E1 隔离 P2 只需关 P1、P7（P4 保持开启）；E2 隔离 P6 时只需再关 P4。
# 此时若先复供 E1（重开 P1/P7），水会经 A-P7 直达 D —— E2 故障管段 P6 的
# D 端重新进水，必须拒绝；先复供 E2 则安全。
LOOP_NETWORK = {
    "nodes": [
        {"id": "S", "type": "source"},
        {"id": "A", "type": "junction"},
        {"id": "B", "type": "junction"},
        {"id": "D", "type": "junction"},
        {"id": "U1", "type": "sink"},
        {"id": "U2", "type": "sink"},
    ],
    "pipes": [
        {"id": "P1", "from": "S", "to": "A", "has_valve": True},
        {"id": "P2", "from": "A", "to": "B", "has_valve": True},
        {"id": "P3", "from": "B", "to": "U1", "has_valve": True},
        {"id": "P4", "from": "B", "to": "D", "has_valve": True},
        {"id": "P6", "from": "D", "to": "U2", "has_valve": False},
        {"id": "P7", "from": "A", "to": "D", "has_valve": True},
    ],
}


class RestorationApiTests(unittest.TestCase):
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

    def _submit(self, fault, closes, version):
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": fault, "close_pipe_ids": closes, "version": version},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_preview_and_confirm_single_event_restoration(self):
        self.client.post("/api/network/import", json=VALID_NETWORK)
        self._submit("P6", ["P4"], 1)  # -> version 2, event 1 pending

        # 预演（只读，不改状态）
        r = self.client.post(
            "/api/restoration/preview", json={"event_id": 1, "version": 2}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["version_match"])
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["fault_pipe_id"], "P6")
        self.assertEqual(body["closed_pipe_ids"], ["P4"])
        self.assertTrue(body["safe"])
        self.assertEqual(body["restored_sink_ids"], ["U3"])
        self.assertEqual(body["issues"], [])
        # 预演不改状态：阀门仍关、版本不变
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.version, 2)

        # 确认复供
        r = self.client.post(
            "/api/restoration/confirm", json={"event_id": 1, "version": 2}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 3)
        self.assertEqual(body["status"], "restored")
        self.assertEqual(body["reopened_pipe_ids"], ["P4"])
        self.assertEqual(body["restored_sink_ids"], ["U3"])
        # 阀位重开
        self.assertFalse(STORE.pipes["P4"].valve_closed)

        # 历史中事件为 restored
        events = self.client.get("/api/isolation/events").json()
        ev = next(e for e in events if e["id"] == 1)
        self.assertEqual(ev["status"], "restored")
        self.assertIsNotNone(ev["restored_at"])

    def test_confirm_unknown_event_404(self):
        self.client.post("/api/network/import", json=VALID_NETWORK)
        for path in ("/api/restoration/preview", "/api/restoration/confirm"):
            r = self.client.post(path, json={"event_id": 999, "version": 1})
            self.assertEqual(r.status_code, 404, path)
            self.assertEqual(r.json()["error"]["code"], "EVENT_NOT_FOUND", path)

    def test_confirm_already_restored_409_and_no_change(self):
        self.client.post("/api/network/import", json=VALID_NETWORK)
        self._submit("P6", ["P4"], 1)  # v2
        r = self.client.post(
            "/api/restoration/confirm", json={"event_id": 1, "version": 2}
        )
        self.assertEqual(r.status_code, 200)  # v3

        # 再次预演 / 确认 -> 409，无部分变更
        r = self.client.post(
            "/api/restoration/preview", json={"event_id": 1, "version": 3}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "EVENT_ALREADY_RESTORED")
        r = self.client.post(
            "/api/restoration/confirm", json={"event_id": 1, "version": 3}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "EVENT_ALREADY_RESTORED")
        self.assertEqual(STORE.version, 3)

    def test_confirm_stale_version_409_and_atomic(self):
        self.client.post("/api/network/import", json=VALID_NETWORK)
        self._submit("P6", ["P4"], 1)  # v2
        r = self.client.post(
            "/api/restoration/confirm", json={"event_id": 1, "version": 1}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        # 阀门保持关闭，版本不变
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.version, 2)

    def test_unsafe_restoration_due_to_other_pending_fault(self):
        # E1 隔离 P2：关 P1、P7（P4 开着也能隔离，B/D 侧整体脱离水源）；
        # E2 隔离 P6：只需再关 P4（P1/P7 已由 E1 关闭）。
        # 此时先复供 E1（重开 P1/P7）-> 水经 A-P7 直达 D，E2 故障 P6 的
        # D 端重新进水 -> 拒绝；先复供 E2 则安全。
        self.client.post("/api/network/import", json=LOOP_NETWORK)
        e1 = self._submit("P2", ["P1", "P7"], 1)  # v2
        self.assertEqual(e1["event_id"], 1)
        e2 = self._submit("P6", ["P4"], 2)  # v3
        self.assertEqual(e2["event_id"], 2)

        # 预演复供 E1：不安全
        r = self.client.post(
            "/api/restoration/preview", json={"event_id": 1, "version": 3}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["safe"])
        self.assertTrue(
            any(i["code"] == "RESUPPLY_RISK" and i["pipe_id"] == "P6"
                for i in body["issues"])
        )

        # 确认 -> 422 RESUPPLY_UNSAFE，无部分变更
        r = self.client.post(
            "/api/restoration/confirm", json={"event_id": 1, "version": 3}
        )
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(r.json()["error"]["code"], "RESUPPLY_UNSAFE")
        self.assertTrue(STORE.pipes["P1"].valve_closed)
        self.assertTrue(STORE.pipes["P7"].valve_closed)
        self.assertEqual(STORE.version, 3)

        # 先复供 E2：安全（P1/P7 仍关，D 侧无进水），无新增恢复用水点
        r = self.client.post(
            "/api/restoration/confirm", json={"event_id": 2, "version": 3}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["restored_sink_ids"], [])
        # v4，P4 重开，P1/P7 仍关
        self.assertFalse(STORE.pipes["P4"].valve_closed)
        self.assertTrue(STORE.pipes["P1"].valve_closed)
        self.assertTrue(STORE.pipes["P7"].valve_closed)

        # E2 已复供后，E1 复供安全：U1、U2 均恢复
        r = self.client.post(
            "/api/restoration/preview", json={"event_id": 1, "version": 4}
        )
        self.assertTrue(r.json()["safe"])
        self.assertEqual(r.json()["restored_sink_ids"], ["U1", "U2"])
        r = self.client.post(
            "/api/restoration/confirm", json={"event_id": 1, "version": 4}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 5)
        self.assertFalse(STORE.pipes["P1"].valve_closed)
        self.assertFalse(STORE.pipes["P7"].valve_closed)

    def test_events_cleared_after_reimport_old_event_invalid(self):
        self.client.post("/api/network/import", json=VALID_NETWORK)
        self._submit("P6", ["P4"], 1)
        self.client.post("/api/network/import", json=VALID_NETWORK)
        r = self.client.post(
            "/api/restoration/confirm", json={"event_id": 1, "version": 1}
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "EVENT_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
