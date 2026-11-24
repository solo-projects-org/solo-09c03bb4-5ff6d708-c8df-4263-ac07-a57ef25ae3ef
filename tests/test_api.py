"""HTTP 层集成测试：事务性重编、失败回滚、锁定保留。"""

import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("FILM_DB_PATH", str(db_path))
    monkeypatch.delenv("SEED_SAMPLE", raising=False)

    import app.models as models
    import app.main as main

    importlib.reload(models)
    importlib.reload(main)
    models.init_db()
    with TestClient(main.app) as c:
        yield c


def _two_scan_payload():
    return {
        "name": "reel-1",
        "first_frame": 1,
        "last_frame": 240,
        "scans": [
            {
                "filename": "scan01.dpx",
                "offset": 0,
                "segments": [{"start_frame": 1, "end_frame": 120}],
            },
            {
                "filename": "scan02.dpx",
                "offset": -80,
                "segments": [{"start_frame": 81, "end_frame": 240}],
            },
        ],
    }


def test_create_roll_and_get_manifest(client):
    resp = client.post("/rolls", json=_two_scan_payload())
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["switch_count"] == 1
    assert [r["filename"] for r in data["runs"]] == ["scan01.dpx", "scan02.dpx"]
    assert data["switches"][0]["overlap_frames"] == [119, 120]

    rid = data["roll_id"]
    got = client.get(f"/rolls/{rid}/manifest").json()
    assert got["last_frame"] == 240
    # 整卷覆盖校验
    assert got["runs"][0]["orig_start"] == 1
    assert got["runs"][-1]["orig_end"] == 240


def test_failed_create_writes_nothing(client):
    payload = _two_scan_payload()
    payload["scans"][1]["segments"] = [{"start_frame": 130, "end_frame": 240}]
    resp = client.post("/rolls", json=payload)
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["error"] == "planning_failed"
    assert body["first_gap_frame"] == 121
    assert client.get("/rolls").json() == []


def test_failed_replan_keeps_old_records_and_manifest(client):
    rid = client.post("/rolls", json=_two_scan_payload()).json()["roll_id"]
    before = client.get(f"/rolls/{rid}/manifest").json()

    # 补交的文件覆盖不足 => 409
    bad = [
        {
            "filename": "scan01.dpx",
            "offset": 0,
            "segments": [{"start_frame": 1, "end_frame": 100}],
        },
        {
            "filename": "scan03.dpx",
            "offset": 0,
            "segments": [{"start_frame": 200, "end_frame": 240}],
        },
    ]
    resp = client.put(f"/rolls/{rid}/scans", json=bad)
    assert resp.status_code == 409
    assert resp.json()["detail"]["first_gap_frame"] == 101

    # 旧扫描记录与旧清单原样生效
    detail = client.get(f"/rolls/{rid}").json()
    assert {s["filename"] for s in detail["scans"]} == {"scan01.dpx", "scan02.dpx"}
    after = client.get(f"/rolls/{rid}/manifest").json()
    assert after == before


def test_successful_supplementary_scan_replans(client):
    rid = client.post("/rolls", json=_two_scan_payload()).json()["roll_id"]
    new_scans = [
        {
            "filename": "scan01.dpx",
            "offset": 0,
            "segments": [{"start_frame": 1, "end_frame": 120}],
        },
        {
            "filename": "scan02.dpx",
            "offset": -80,
            "segments": [{"start_frame": 81, "end_frame": 200}],
        },
        {
            "filename": "scan03.dpx",
            "offset": -200,
            "segments": [{"start_frame": 199, "end_frame": 240}],
        },
    ]
    resp = client.put(f"/rolls/{rid}/scans", json=new_scans)
    assert resp.status_code == 200, resp.text
    data = resp.json()["manifest"]
    assert data["runs"][-1]["filename"] == "scan03.dpx"
    # 档内帧号保留映射：scan03 原片 201 -> 档内 401
    last = data["runs"][-1]
    assert last["file_start"] == last["orig_start"] + 200


def test_lock_preserved_on_replan(client):
    rid = client.post("/rolls", json=_two_scan_payload()).json()["roll_id"]
    resp = client.post(
        f"/rolls/{rid}/locks",
        json={"start_frame": 20, "end_frame": 40, "filename": "scan01.dpx"},
    )
    assert resp.status_code == 201, resp.text
    manifest = resp.json()["manifest"]
    locked = [r for r in manifest["runs"] if r["locked"]]
    assert locked and all(r["filename"] == "scan01.dpx" for r in locked)
    target = next(r for r in locked if r["orig_start"] <= 20 <= r["orig_end"])
    assert target["file_start"] <= 20 <= target["file_end"]

    # 再次更正可用段后重编，锁定段归属与档内帧号不变
    corrected = _two_scan_payload()["scans"]
    corrected[0]["segments"] = [{"start_frame": 1, "end_frame": 130}]
    repl = client.put(f"/rolls/{rid}/scans", json=corrected).json()["manifest"]
    kept = next(r for r in repl["runs"] if r["orig_start"] <= 20 <= r["orig_end"])
    assert kept["filename"] == "scan01.dpx"
    assert kept["file_start"] == target["file_start"]
    assert kept["locked"] is True


def test_lock_that_makes_plan_unsolvable_is_rejected(client):
    rid = client.post("/rolls", json=_two_scan_payload()).json()["roll_id"]
    resp = client.post(
        f"/rolls/{rid}/locks",
        json={"start_frame": 200, "end_frame": 210, "filename": "scan01.dpx"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["first_gap_frame"] == 200
    # 锁定未写入
    detail = client.get(f"/rolls/{rid}").json()
    assert detail["locks"] == []


def test_unlock_and_delete_lock_flow(client):
    rid = client.post("/rolls", json=_two_scan_payload()).json()["roll_id"]
    lock = client.post(
        f"/rolls/{rid}/locks",
        json={"start_frame": 20, "end_frame": 40, "filename": "scan01.dpx"},
    ).json()["manifest"]
    assert any(r["locked"] for r in lock["runs"])

    lock_id = client.get(f"/rolls/{rid}").json()["locks"][0]["id"]
    resp = client.delete(f"/rolls/{rid}/locks/{lock_id}")
    assert resp.status_code == 200
    assert not any(r["locked"] for r in resp.json()["manifest"]["runs"])


def test_swagger_and_openapi_available(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/rolls/{roll_id}/scans" in schema["paths"]
