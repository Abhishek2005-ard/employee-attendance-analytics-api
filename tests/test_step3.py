import os
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient

from app.main import app, db, IST

client = TestClient(app)

TEST_CODES = [f"EMP800{i}" for i in range(1, 10)]


@pytest.fixture(autouse=True)
def setup_teardown_db():
    db.employees.delete_many({"emp_code": {"$in": TEST_CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": TEST_CODES}})
    yield
    db.employees.delete_many({"emp_code": {"$in": TEST_CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": TEST_CODES}})


# --------------------------------------------------------------------------- #
# Part A: Punch-Out Tests
# --------------------------------------------------------------------------- #
def test_punch_out_normal_and_sample1_calculations():
    # Create employee (09:30 - 18:30)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8001",
            "name": "Normal Worker",
            "email": "worker@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    # Punch in 09:28 IST (03:58 UTC) on 2026-07-06
    in_dt = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    client.post("/attendance/punch-in", json={"emp_code": "EMP8001", "punched_at": int(in_dt.timestamp() * 1000)})

    # Punch out 18:35 IST (13:05 UTC) on 2026-07-06
    out_dt = datetime(2026, 7, 6, 18, 35, 0, tzinfo=IST)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP8001", "punched_at": int(out_dt.timestamp() * 1000)})

    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP8001"
    assert data["date"] == "2026-07-06"
    assert data["work_hours"] == 9.12
    assert data["late_minutes"] == 0
    assert data["overtime_minutes"] == 0  # 5 min is under 30 min threshold
    assert data["half_day"] is False
    assert "_id" not in data
    assert "id" not in data


def test_punch_out_late_and_overtime():
    # Create employee
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8002",
            "name": "Late Overtime",
            "email": "lateot@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    # Punch in 10:05 IST (35 min late)
    in_dt = datetime(2026, 7, 6, 10, 5, 0, tzinfo=IST)
    client.post("/attendance/punch-in", json={"emp_code": "EMP8002", "punched_at": int(in_dt.timestamp() * 1000)})

    # Punch out 19:10 IST (40 min overtime)
    out_dt = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP8002", "punched_at": int(out_dt.timestamp() * 1000)})

    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 35
    assert data["overtime_minutes"] == 40
    assert data["work_hours"] == 9.08
    assert data["half_day"] is False


def test_punch_out_half_day():
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8003",
            "name": "Half Day Worker",
            "email": "half@example.com",
            "department": "Sales",
            "joined_on": "2026-01-01",
        },
    )
    # Punch in 09:30 IST
    in_dt = datetime(2026, 7, 7, 9, 30, 0, tzinfo=IST)
    client.post("/attendance/punch-in", json={"emp_code": "EMP8003", "punched_at": int(in_dt.timestamp() * 1000)})

    # Punch out 13:00 IST (3.5 hours worked)
    out_dt = datetime(2026, 7, 7, 13, 0, 0, tzinfo=IST)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP8003", "punched_at": int(out_dt.timestamp() * 1000)})
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 3.5
    assert data["half_day"] is True


def test_punch_out_overnight_shift():
    # Overnight shift (22:00 - 06:00)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8004",
            "name": "Overnight Worker",
            "email": "nightworker@example.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-01",
        },
    )
    # Punch in 21:55 IST on 2026-07-06
    in_dt = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
    client.post("/attendance/punch-in", json={"emp_code": "EMP8004", "punched_at": int(in_dt.timestamp() * 1000)})

    # Punch out 06:40 IST on 2026-07-07 (across midnight)
    out_dt = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP8004", "punched_at": int(out_dt.timestamp() * 1000)})

    assert res.status_code == 200
    data = res.json()
    assert data["date"] == "2026-07-06"
    assert data["overtime_minutes"] == 40
    assert data["work_hours"] == 8.75


def test_punch_out_error_conditions():
    # Unknown employee -> 404
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404

    # Existing employee with no punch-in -> 404
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8005",
            "name": "No Punch",
            "email": "nopunch@example.com",
            "department": "HR",
            "joined_on": "2026-01-01",
        },
    )
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP8005"}).status_code == 404

    # Punch in
    in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    client.post("/attendance/punch-in", json={"emp_code": "EMP8005", "punched_at": int(in_dt.timestamp() * 1000)})

    # Punch out with timestamp <= punch_in -> 422
    early_out = datetime(2026, 7, 6, 9, 29, 0, tzinfo=IST)
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP8005", "punched_at": int(early_out.timestamp() * 1000)}).status_code == 422

    # Punch out with timestamp > 24 hours -> 422
    too_late = datetime(2026, 7, 7, 10, 0, 0, tzinfo=IST)
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP8005", "punched_at": int(too_late.timestamp() * 1000)}).status_code == 422

    # Valid punch out
    valid_out = datetime(2026, 7, 6, 18, 30, 0, tzinfo=IST)
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP8005", "punched_at": int(valid_out.timestamp() * 1000)}).status_code == 200

    # Second punch out attempt on already closed record -> 409
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP8005", "punched_at": int(valid_out.timestamp() * 1000)}).status_code == 409


# --------------------------------------------------------------------------- #
# Part B: Regularization (PATCH) Tests
# --------------------------------------------------------------------------- #
def test_regularize_punch_in_and_recompute():
    # Setup employee and record with 35 min late
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8006",
            "name": "Reg Test",
            "email": "reg@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    in_dt = datetime(2026, 7, 7, 10, 5, 0, tzinfo=IST)
    out_dt = datetime(2026, 7, 7, 18, 30, 0, tzinfo=IST)
    client.post("/attendance/punch-in", json={"emp_code": "EMP8006", "punched_at": int(in_dt.timestamp() * 1000)})
    client.post("/attendance/punch-out", json={"emp_code": "EMP8006", "punched_at": int(out_dt.timestamp() * 1000)})

    # Correct punch-in to 09:28 IST
    new_in = datetime(2026, 7, 7, 9, 28, 0, tzinfo=IST)
    res = client.patch(
        "/attendance/EMP8006/2026-07-07",
        json={
            "punch_in": int(new_in.timestamp() * 1000),
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 0
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "biometric glitch"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert "punch_in" in h["changes"]
    assert "_id" not in data
    assert "id" not in data


def test_regularize_status_to_leave():
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8007",
            "name": "Leave Test",
            "email": "leave@example.com",
            "department": "Sales",
            "joined_on": "2026-01-01",
        },
    )
    in_dt = datetime(2026, 7, 8, 9, 30, 0, tzinfo=IST)
    client.post("/attendance/punch-in", json={"emp_code": "EMP8007", "punched_at": int(in_dt.timestamp() * 1000)})

    # Regularize to LEAVE
    res = client.patch(
        "/attendance/EMP8007/2026-07-08",
        json={
            "status": "LEAVE",
            "reason": "approved emergency leave",
            "regularized_by": "manager",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "LEAVE"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["late_minutes"] == 0
    assert data["work_hours"] is None
    assert len(data["history"]) == 1


def test_regularize_validations():
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8008",
            "name": "Val Test",
            "email": "val@example.com",
            "department": "HR",
            "joined_on": "2026-01-01",
        },
    )
    in_dt = datetime(2026, 7, 8, 9, 30, 0, tzinfo=IST)
    client.post("/attendance/punch-in", json={"emp_code": "EMP8008", "punched_at": int(in_dt.timestamp() * 1000)})

    # 1. 404 for unknown record / employee
    assert client.patch("/attendance/EMP9999/2026-07-08", json={"reason": "test reason", "regularized_by": "admin"}).status_code == 404
    assert client.patch("/attendance/EMP8008/2026-07-01", json={"reason": "test reason", "regularized_by": "admin"}).status_code == 404

    # 2. 422: setting status ABSENT while supplying punch_in
    res_absent_punch = client.patch(
        "/attendance/EMP8008/2026-07-08",
        json={
            "status": "ABSENT",
            "punch_in": int(in_dt.timestamp() * 1000),
            "reason": "invalid combination",
            "regularized_by": "admin",
        },
    )
    assert res_absent_punch.status_code == 422

    # 3. 422: punch_in evaluated to a different date
    wrong_day_in = datetime(2026, 7, 9, 9, 30, 0, tzinfo=IST)
    res_wrong_date = client.patch(
        "/attendance/EMP8008/2026-07-08",
        json={
            "punch_in": int(wrong_day_in.timestamp() * 1000),
            "reason": "date mismatch",
            "regularized_by": "admin",
        },
    )
    assert res_wrong_date.status_code == 422

    # 4. 422: no changes requested
    res_no_change = client.patch(
        "/attendance/EMP8008/2026-07-08",
        json={
            "status": "PRESENT",  # already PRESENT
            "reason": "no actual changes",
            "regularized_by": "admin",
        },
    )
    assert res_no_change.status_code == 422


def test_regularize_concurrency_race():
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8009",
            "name": "Race Worker",
            "email": "race@example.com",
            "department": "QA",
            "joined_on": "2026-01-01",
        },
    )
    in_dt = datetime(2026, 7, 8, 9, 30, 0, tzinfo=IST)
    client.post("/attendance/punch-in", json={"emp_code": "EMP8009", "punched_at": int(in_dt.timestamp() * 1000)})

    # First regularization succeeds
    res1 = client.patch(
        "/attendance/EMP8009/2026-07-08",
        json={"status": "WFH", "reason": "remote work approval", "regularized_by": "mgr1"},
    )
    assert res1.status_code == 200

    # Second regularization with new change also succeeds and appends to history
    res2 = client.patch(
        "/attendance/EMP8009/2026-07-08",
        json={"status": "ON_DUTY", "reason": "client visit approval", "regularized_by": "mgr2"},
    )
    assert res2.status_code == 200
    assert len(res2.json()["history"]) == 2
    assert res2.json()["history"][0]["by"] == "mgr1"
    assert res2.json()["history"][1]["by"] == "mgr2"
