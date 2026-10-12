import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from app.main import app, db, IST

client = TestClient(app)

TEST_CODES = [f"EMP700{i}" for i in range(1, 10)]


@pytest.fixture(autouse=True)
def setup_teardown_db():
    db.employees.delete_many({"emp_code": {"$in": TEST_CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": TEST_CODES}})
    yield
    db.employees.delete_many({"emp_code": {"$in": TEST_CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": TEST_CODES}})


# --------------------------------------------------------------------------- #
# 1. Employee Monthly Analytics Tests
# --------------------------------------------------------------------------- #
def test_employee_monthly_existing():
    # EMP0001 joined 2026-01-05. In 2026-07 there are 23 working days (Mon-Fri).
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 2.0  # July 6 and July 7
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    assert data["total_overtime_minutes"] == 0
    # 2.0 / 23 * 100 = 8.6956... -> 8.70%
    assert data["attendance_pct"] == 8.70


def test_employee_monthly_mid_month_joiner():
    # EMP7001 joins on Wednesday 2026-07-15.
    client.post(
        "/employees",
        json={
            "emp_code": "EMP7001",
            "name": "Mid Month Joiner",
            "email": "mid@example.com",
            "department": "Engineering",
            "joined_on": "2026-07-15",
        },
    )
    # Working days from July 15 to July 31 (inclusive) Mon-Fri: 13 working days
    res = client.get("/analytics/employees/EMP7001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 13
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] == 0.0


def test_employee_monthly_unknown_and_validation():
    assert client.get("/analytics/employees/EMP9999/monthly?month=2026-07").status_code == 404
    assert client.get("/analytics/employees/EMP0001/monthly?month=invalid").status_code == 422


# --------------------------------------------------------------------------- #
# 2. Department Summary Analytics Tests
# --------------------------------------------------------------------------- #
def test_department_summary():
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    depts = {item["department"]: item for item in data["items"]}
    assert "Engineering" in depts
    assert "Sales" in depts
    assert "Support" in depts

    eng = depts["Engineering"]
    # EMP0001 (joined Jan 2026) and EMP0002 (joined July 13 2026) -> headcount = 2
    assert eng["headcount"] == 2
    assert eng["present_days"] == 3.0  # July 6 (EMP0001), July 7 (EMP0001), July 14 (EMP0002)
    assert eng["avg_work_hours"] == 9.04


def test_department_summary_future_joiner_excluded():
    # Create an employee who joins in August 2026
    client.post(
        "/employees",
        json={
            "emp_code": "EMP7002",
            "name": "August Joiner",
            "email": "august@example.com",
            "department": "AnalyticsQA",
            "joined_on": "2026-08-01",
        },
    )
    # Summary for July 2026: headcount is 0, so department is omitted per contract
    res_july = client.get("/analytics/departments/summary?month=2026-07&department=AnalyticsQA")
    assert res_july.status_code == 200
    assert len(res_july.json()["items"]) == 0

    # Summary for August 2026: headcount is 1
    res_aug = client.get("/analytics/departments/summary?month=2026-08&department=AnalyticsQA")
    assert res_aug.status_code == 200
    assert len(res_aug.json()["items"]) == 1
    assert res_aug.json()["items"][0]["headcount"] == 1


# --------------------------------------------------------------------------- #
# 3. Leaderboard Late Analytics Tests
# --------------------------------------------------------------------------- #
def test_late_leaderboard_standard_competition_ranking_ties():
    # Create 3 employees in DeptTie
    for i in [1, 2, 3]:
        client.post(
            "/employees",
            json={
                "emp_code": f"EMP700{i}",
                "name": f"Tie Worker {i}",
                "email": f"tieworker{i}@example.com",
                "department": "DeptTie",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )

    # EMP7001 and EMP7002 have 30 late minutes (tied for rank 1)
    # EMP7003 has 15 late minutes (should be rank 3, since rank 2 is skipped!)
    dt1 = datetime(2026, 7, 10, 10, 0, 0, tzinfo=IST)
    dt2 = datetime(2026, 7, 10, 9, 45, 0, tzinfo=IST)

    client.post("/attendance/punch-in", json={"emp_code": "EMP7001", "punched_at": int(dt1.timestamp() * 1000)})
    client.post("/attendance/punch-in", json={"emp_code": "EMP7002", "punched_at": int(dt1.timestamp() * 1000)})
    client.post("/attendance/punch-in", json={"emp_code": "EMP7003", "punched_at": int(dt2.timestamp() * 1000)})

    res = client.get("/analytics/leaderboard/late?month=2026-07&department=DeptTie")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    # Standard competition ranking: 1, 1, 3
    assert items[0]["rank"] == 1
    assert items[0]["emp_code"] == "EMP7001"
    assert items[0]["total_late_minutes"] == 30

    assert items[1]["rank"] == 1
    assert items[1]["emp_code"] == "EMP7002"
    assert items[1]["total_late_minutes"] == 30

    assert items[2]["rank"] == 3
    assert items[2]["emp_code"] == "EMP7003"
    assert items[2]["total_late_minutes"] == 15

    # Test limit=1: since rank 1 has a tie, both items whose rank <= 1 must be returned (more than limit!)
    res_lim1 = client.get("/analytics/leaderboard/late?month=2026-07&department=DeptTie&limit=1")
    assert res_lim1.status_code == 200
    assert len(res_lim1.json()["items"]) == 2


# --------------------------------------------------------------------------- #
# 4. Department Trend Analytics Tests
# --------------------------------------------------------------------------- #
def test_department_trend_gap_filling_and_moving_avg():
    # Test Engineering from 2026-07-05 to 2026-07-10 (6 days)
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-10")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    items = data["items"]
    assert len(items) == 6

    # July 5 (Sunday): weekend
    assert items[0]["date"] == "2026-07-05"
    assert items[0]["is_working_day"] is False
    assert items[0]["attendance_rate"] is None
    assert items[0]["moving_avg_7d"] is None

    # July 6 (Monday): present_count = 1.0, headcount = 1, rate = 1.0, moving_avg_7d = 1.0
    assert items[1]["date"] == "2026-07-06"
    assert items[1]["is_working_day"] is True
    assert items[1]["present_count"] == 1.0
    assert items[1]["headcount"] == 1
    assert items[1]["attendance_rate"] == 1.0
    assert items[1]["moving_avg_7d"] == 1.0

    # July 8 (Wednesday): no logs recorded -> rate = 0.0
    assert items[3]["date"] == "2026-07-08"
    assert items[3]["is_working_day"] is True
    assert items[3]["present_count"] == 0.0
    assert items[3]["attendance_rate"] == 0.0
    # moving average over [1.0, 1.0, 0.0] -> 2.0 / 3 = 0.6667
    assert items[3]["moving_avg_7d"] == 0.6667


def test_department_trend_errors():
    # Unknown department -> 404
    assert client.get("/analytics/departments/NonExistentDept/trend?from=2026-07-01&to=2026-07-05").status_code == 404

    # to < from -> 422
    assert client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-05").status_code == 422

    # range > 92 days -> 422
    assert client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01").status_code == 422
