#!/usr/bin/env python3
"""复供（恢复供水）调用示例（仅依赖标准库）。

用法：
    python3 examples/restore_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/sample_restore_network.json）：

    S --P1-- A --P2-- B --P4(阀)-- D --P6(无阀,故障)--> U3
                    |       |                      |
                   P3      P5(阀)                  P7(阀)
                    |       |                      |
                   U1      U2                      E --P8(无阀,故障)--> U4

演示流程：
  1. 整网导入（旧事件全部失效，版本重置为 1）
  2. 提交隔离事件 #1：关 P4 隔离 P6（U3、U4 同时失供）
  3. 在隔离区内提交隔离事件 #2：P8 故障（P4 已断水，无需再关阀）
  4. 复供预演 #1：重开 P4 会让 P8 重新进水 -> safe=false，拒绝确认
  5. 先复供 #2：无阀门可重开，安全
  6. 再复供 #1：P4 重开，U3/U4 恢复供水
  7. 已复供事件再次操作 -> 409；历史中区分 pending/restored
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "BASE_URL", "http://localhost:8000"
)
SAMPLE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sample_restore_network.json"
)


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
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网（版本 1；历史清空）
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入复供示例管网", status, body)
    assert status == 200, body

    # 2. 隔离事件 #1：关 P4 隔离故障管段 P6
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    show("2. 提交隔离 #1（关 P4，U3/U4 失供）", status, body)
    assert status == 200 and body["event_id"] == 1, body
    version = body["version"]  # 2

    # 3. 隔离事件 #2：同一隔离区内的 P8 故障；P4 已关，无需再关阀
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P8", "close_pipe_ids": [], "version": version},
    )
    show("3. 提交隔离 #2（P8 故障，无需再关阀；U4 已失供不重复计入）", status, body)
    assert status == 200 and body["event_id"] == 2, body
    version = body["version"]  # 3

    # 4. 复供预演 #1（应提示重新进水风险，且不改任何状态）
    status, body = call(
        "POST", "/api/isolation/events/1/restore/preview", {"version": version}
    )
    show("4. 复供预演 #1（预期 safe=false，RESTORE_REWATER_RISK）", status, body)
    assert status == 200 and body["safe"] is False, body
    assert body["recoverable_sink_ids"] == ["U3"]
    assert body["still_blocked_sink_ids"] == ["U4"]

    # 4b. 仍强行确认 -> 422 RESTORE_UNSAFE，不产生部分变更
    status, body = call(
        "POST", "/api/isolation/events/1/restore/confirm", {"version": version}
    )
    show("4b. 强确认 #1（预期 422 RESTORE_UNSAFE）", status, body)
    assert status == 422 and body["error"]["code"] == "RESTORE_UNSAFE", body

    # 5. 先复供 #2：无阀门可重开；P8 仍被 P4 隔离，安全
    status, body = call(
        "POST", "/api/isolation/events/2/restore/preview", {"version": version}
    )
    show("5a. 复供预演 #2（预期 safe=true，reopened 为空）", status, body)
    assert status == 200 and body["safe"] is True, body

    status, body = call(
        "POST", "/api/isolation/events/2/restore/confirm", {"version": version}
    )
    show("5b. 确认复供 #2（版本 +1，无阀门重开）", status, body)
    assert status == 200 and body["reopened_pipe_ids"] == [], body
    version = body["version"]  # 4

    # 6. 再复供 #1：其余待修复故障已清空，P4 可安全重开
    status, body = call(
        "POST", "/api/isolation/events/1/restore/preview", {"version": version}
    )
    show("6a. 复供预演 #1（预期 safe=true，U3/U4 恢复）", status, body)
    assert status == 200 and body["safe"] is True, body
    assert body["recoverable_sink_ids"] == ["U3", "U4"]

    status, body = call(
        "POST", "/api/isolation/events/1/restore/confirm", {"version": version}
    )
    show("6b. 确认复供 #1（P4 重开，版本 +1）", status, body)
    assert status == 200 and body["reopened_pipe_ids"] == ["P4"], body

    # 7. 重复复供 / 历史
    status, body = call(
        "POST",
        "/api/isolation/events/1/restore/confirm",
        {"version": body["version"]},
    )
    show("7a. 重复复供 #1（预期 409 EVENT_ALREADY_RESTORED）", status, body)
    assert status == 409 and body["error"]["code"] == "EVENT_ALREADY_RESTORED", body

    status, body = call("GET", "/api/isolation/events")
    show("7b. 隔离/复供历史（含 status 字段）", status, body)
    assert status == 200
    statuses = {e["id"]: e["status"] for e in body}
    assert statuses == {1: "restored", 2: "restored"}, statuses

    print("\n复供示例全部执行完成。")


if __name__ == "__main__":
    main()
