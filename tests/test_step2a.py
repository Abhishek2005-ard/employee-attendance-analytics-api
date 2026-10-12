import os
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import app, db, IST

client = TestClient(app)

TEST_CODES = [f"EMP900{i}" for i in range(1, 10)]


@pytest.fixture(autouse=True)
def setup_teardown_db():
    # Clean up test employees and attendance records before and after each test
    db.employees.delete_many({"emp_code": {"$in": TEST_CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": TEST_CODES}})
    yield
    db.employees.delete_many({"emp_code": {"$in": TEST_CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": TEST_CODES}})


# --------------------------------------------------------------------------- #
# 1. Health Endpoint Tests
# --------------------------------------------------------------------------- #
def test_health_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_database_down():
    with patch("app.main.admin_db.command", side_effect=Exception("Connection refused")):
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["detail"] == "Database unavailable"


# --------------------------------------------------------------------------- #
# 2. Employees Endpoints Tests
# --------------------------------------------------------------------------- #
def test_create_employee_success():
    payload = {
        "emp_code": "EMP9001",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    response = client.post("/employees", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["emp_code"] == "EMP9001"
    assert "_id" not in data
    assert "id" not in data
    assert isinstance(data["created_at"], int)
    assert data["created_at"] >= 100000000000


def test_create_employee_duplicate_409():
    payload = {
        "emp_code": "EMP9002",
        "name": "User 1",
        "email": "user1@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    res1 = client.post("/employees", json=payload)
    assert res1.status_code == 201

    # Second creation with identical emp_code must return 409
    res2 = client.post("/employees", json=payload)
    assert res2.status_code == 409


def test_create_employee_validation_422():
    # Bad emp_code pattern (not EMP followed by 4-6 digits)
    bad_code = {
        "emp_code": "INVALID_CODE",
        "name": "Bad Code",
        "email": "bad@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
    }
    assert client.post("/employees", json=bad_code).status_code == 422

    # Equal shift_start and shift_end
    equal_shifts = {
        "emp_code": "EMP9003",
        "name": "Equal Shifts",
        "email": "equal@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-01",
    }
    assert client.post("/employees", json=equal_shifts).status_code == 422


def test_list_employees_pagination_and_sorting():
    # Insert 3 test employees in a dedicated test department
    for i in [3, 1, 2]:
        res = client.post(
            "/employees",
            json={
                "emp_code": f"EMP900{i}",
                "name": f"User {i}",
                "email": f"user{i}@example.com",
                "department": "SpecialOps",
                "joined_on": "2026-01-01",
            },
        )
        assert res.status_code == 201

    # List department employees: should sort EMP9001, EMP9002, EMP9003
    res = client.get("/employees?department=SpecialOps&page=1&page_size=2")
    assert res.status_code == 200
    data = res.json()
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert data["total"] == 3
    # Ensure page 1 does NOT skip results
    assert len(data["items"]) == 2
    assert data["items"][0]["emp_code"] == "EMP9001"
    assert data["items"][1]["emp_code"] == "EMP9002"
    assert "_id" not in data["items"][0]
    assert "id" not in data["items"][0]

    # Page 2
    res2 = client.get("/employees?department=SpecialOps&page=2&page_size=2")
    assert res2.status_code == 200
    data2 = res2.json()
    assert len(data2["items"]) == 1
    assert data2["items"][0]["emp_code"] == "EMP9003"


# --------------------------------------------------------------------------- #
# 3. Attendance Punch-In Tests
# --------------------------------------------------------------------------- #
def test_punch_in_employee_not_found_404():
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res.status_code == 404
    assert res.json()["detail"] == "employee not found"


def test_punch_in_success_and_duplicate_409():
    # Create employee
    emp_res = client.post(
        "/employees",
        json={
            "emp_code": "EMP9004",
            "name": "Puncher",
            "email": "puncher@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    assert emp_res.status_code == 201

    # Punch in 2026-07-06 09:40:01 IST (10 min late)
    dt = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    ts_ms = int(dt.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={"emp_code": "EMP9004", "punched_at": ts_ms})
    assert res.status_code == 201
    record = res.json()
    assert record["emp_code"] == "EMP9004"
    assert record["date"] == "2026-07-06"
    assert record["status"] == "PRESENT"
    assert record["late_minutes"] == 10
    assert record["work_hours"] is None
    assert record["half_day"] is False
    assert record["history"] == []
    assert "_id" not in record
    assert "id" not in record

    # Duplicate punch-in for same date must return 409
    res_dup = client.post("/attendance/punch-in", json={"emp_code": "EMP9004", "punched_at": ts_ms})
    assert res_dup.status_code == 409


def test_punch_in_overnight_shift_date_assignment():
    # Create overnight shift employee (22:00 - 06:00)
    emp_res = client.post(
        "/employees",
        json={
            "emp_code": "EMP9005",
            "name": "Night Owl",
            "email": "night@example.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-01",
        },
    )
    assert emp_res.status_code == 201

    # Punch in at 05:30 on 2026-07-07 (< 06:00 shift end) -> belongs to 2026-07-06 shift!
    dt_early = datetime(2026, 7, 7, 5, 30, 0, tzinfo=IST)
    ts_early = int(dt_early.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={"emp_code": "EMP9005", "punched_at": ts_early})
    assert res.status_code == 201
    assert res.json()["date"] == "2026-07-06"


def test_punch_in_invalid_punched_at_422():
    emp_res = client.post(
        "/employees",
        json={
            "emp_code": "EMP9006",
            "name": "Tester",
            "email": "tester@example.com",
            "department": "Support",
            "joined_on": "2026-01-01",
        },
    )
    assert emp_res.status_code == 201

    # Seconds-looking value (< 100000000000)
    res_sec = client.post("/attendance/punch-in", json={"emp_code": "EMP9006", "punched_at": 1783312500})
    assert res_sec.status_code == 422

    # Float value
    res_float = client.post("/attendance/punch-in", json={"emp_code": "EMP9006", "punched_at": 1783312500000.5})
    assert res_float.status_code == 422

    # Invalid status
    res_status = client.post("/attendance/punch-in", json={"emp_code": "EMP9006", "status": "ABSENT"})
    assert res_status.status_code == 422


# --------------------------------------------------------------------------- #
# 4. Attendance List Tests
# --------------------------------------------------------------------------- #
def test_list_attendance_filtering_and_sorting():
    emp_res = client.post(
        "/employees",
        json={
            "emp_code": "EMP9007",
            "name": "Att Tester",
            "email": "att@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )
    assert emp_res.status_code == 201

    # Create two records on different dates
    dt1 = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    dt2 = datetime(2026, 7, 7, 9, 30, 0, tzinfo=IST)
    res1 = client.post("/attendance/punch-in", json={"emp_code": "EMP9007", "punched_at": int(dt1.timestamp() * 1000)})
    assert res1.status_code == 201
    res2 = client.post("/attendance/punch-in", json={"emp_code": "EMP9007", "punched_at": int(dt2.timestamp() * 1000)})
    assert res2.status_code == 201

    # List: sorted date descending (2026-07-07 first)
    res = client.get("/attendance?emp_code=EMP9007")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["date"] == "2026-07-07"
    assert data["items"][1]["date"] == "2026-07-06"
    assert "_id" not in data["items"][0]
    assert "id" not in data["items"][0]

    # Filter by date_from and date_to
    res_filtered = client.get("/attendance?emp_code=EMP9007&date_from=2026-07-07&date_to=2026-07-07")
    assert res_filtered.status_code == 200
    assert res_filtered.json()["total"] == 1
    assert res_filtered.json()["items"][0]["date"] == "2026-07-07"

    # Inverted dates (date_from > date_to) must return 422
    res_inv = client.get("/attendance?date_from=2026-07-08&date_to=2026-07-05")
    assert res_inv.status_code == 422
