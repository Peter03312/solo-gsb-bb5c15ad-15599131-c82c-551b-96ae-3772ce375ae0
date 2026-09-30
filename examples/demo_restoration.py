#!/usr/bin/env python3
"""多故障事件复供顺序示例（仅依赖标准库）。

用法：
    python3 examples/demo_restoration.py [base_url]

演示一个环形管网中两个待修复事件的复供约束：

    S(水源) --P1[阀]-- A --P2[阀]-- B --P3[阀]-- U1(用水点)
                       |             |
                      P7[阀]        P4[阀]
                       |             |
                       D-------------+
                       |
                      P6(无阀, 可故障)
                       |
                      U2(用水点)

1. 事件1：故障 P2（A-B），关 P1、P7 隔离 —— B/D 侧整体脱离水源；
2. 事件2：故障 P6（D-U2），在 P1/P7 已关前提下只需再关 P4 隔离；
3. 此时若先复供事件1（重开 P1/P7），水会经 A-P7 直达 D，
   事件2 的故障管段 P6 的 D 端重新进水 -> 复供预演 safe=false，确认被 422 拒绝；
4. 先复供事件2（重开 P4，P1/P7 仍关）-> 安全；
5. 事件2 已复供后再复供事件1 -> 安全，U1、U2 恢复供水。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "BASE_URL", "http://localhost:8000"
)

NETWORK = {
    "nodes": [
        {"id": "S", "type": "source"},
        {"id": "A", "type": "junction"},
        {"id": "B", "type": "junction"},
        {"id": "D", "type": "junction"},
        {"id": "U1", "type": "sink", "name": "幸福小区"},
        {"id": "U2", "type": "sink", "name": "东郊工业园"},
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


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> None:
    # 1. 整网导入（版本 1）
    status, body = call("POST", "/api/network/import", NETWORK)
    show("1. 导入环形管网", status, body)
    assert status == 200, body

    # 2. 事件1：隔离故障 P2，关 P1、P7（版本 1 -> 2）
    status, e1 = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P2", "close_pipe_ids": ["P1", "P7"], "version": 1},
    )
    show("2. 事件1：隔离 P2（关 P1/P7）", status, e1)
    assert status == 200 and e1["event_id"] == 1 and e1["version"] == 2

    # 3. 事件2：隔离故障 P6，借助已关的 P1/P7，只需再关 P4（版本 2 -> 3）
    status, e2 = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 2},
    )
    show("3. 事件2：隔离 P6（仅关 P4）", status, e2)
    assert status == 200 and e2["event_id"] == 2 and e2["version"] == 3

    # 4. 先复供事件1：预演给出重新进水风险（只读，不改状态）
    status, body = call(
        "POST", "/api/restoration/preview", {"event_id": 1, "version": 3}
    )
    show("4. 复供预演事件1（预期 safe=false，阻断原因指向事件2/P6）", status, body)
    assert status == 200 and body["safe"] is False
    assert any(
        i["code"] == "RESUPPLY_RISK" and i["pipe_id"] == "P6"
        for i in body["issues"]
    )

    # 5. 强行确认事件1 -> 422 RESUPPLY_UNSAFE，无部分变更
    status, body = call(
        "POST", "/api/restoration/confirm", {"event_id": 1, "version": 3}
    )
    show("5. 确认复供事件1（预期 422 RESUPPLY_UNSAFE）", status, body)
    assert status == 422 and body["error"]["code"] == "RESUPPLY_UNSAFE"

    # 6. 按正确顺序：先复供事件2（安全，无新增恢复用水点，版本 3 -> 4）
    status, body = call(
        "POST", "/api/restoration/confirm", {"event_id": 2, "version": 3}
    )
    show("6. 先确认复供事件2（预期成功，仅重开 P4）", status, body)
    assert status == 200 and body["reopened_pipe_ids"] == ["P4"]
    assert body["version"] == 4

    # 7. 事件2 已复供，事件1 复供预演变为安全：U1、U2 恢复
    status, body = call(
        "POST", "/api/restoration/preview", {"event_id": 1, "version": 4}
    )
    show("7. 再预演事件1（预期 safe=true，U1/U2 恢复）", status, body)
    assert status == 200 and body["safe"]
    assert body["restored_sink_ids"] == ["U1", "U2"]

    # 8. 确认复供事件1（版本 4 -> 5，P1/P7 重开）
    status, body = call(
        "POST", "/api/restoration/confirm", {"event_id": 1, "version": 4}
    )
    show("8. 确认复供事件1（预期成功）", status, body)
    assert status == 200 and body["version"] == 5
    assert body["reopened_pipe_ids"] == ["P1", "P7"]

    # 9. 历史：两事件均为 restored
    status, body = call("GET", "/api/isolation/events")
    show("9. 隔离 / 复供历史", status, body)
    assert {e["id"]: e["status"] for e in body} == {1: "restored", 2: "restored"}

    print("\n复供顺序示例执行完成。")


if __name__ == "__main__":
    main()
