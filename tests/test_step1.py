import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from pymongo.errors import DuplicateKeyError

from app.main import (
    IST,
    init_db,
    db,
    truncate_to_seconds,
    epoch_ms_to_datetime,
    datetime_to_epoch_ms,
    is_overnight_shift,
    get_attendance_date,
    get_shift_window,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
)


def test_truncate_to_seconds():
    # 1783312500999 ms -> 1783312500000 ms
    assert truncate_to_seconds(1783312500999) == 1783312500000
    assert truncate_to_seconds(1783312500000) == 1783312500000


def test_epoch_ms_and_datetime_conversions():
    ms = 1783312500999
    dt = epoch_ms_to_datetime(ms)
    assert dt.tzinfo == timezone.utc
    assert dt.microsecond == 0
    # Converted back to ms should reflect whole seconds
    assert datetime_to_epoch_ms(dt) == 1783312500000
    assert datetime_to_epoch_ms(None) is None


def test_is_overnight_shift():
    assert not is_overnight_shift("09:30", "18:30")
    assert is_overnight_shift("22:00", "06:00")
    assert is_overnight_shift("20:00", "20:00")  # shift_end <= shift_start


def test_get_attendance_date_r1():
    # Regular day shift (09:30 - 18:30)
    dt_reg = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    assert get_attendance_date(dt_reg, "09:30", "18:30") == "2026-07-06"

    # Overnight shift (22:00 - 06:00)
    # Punch in 21:55 on 2026-07-06 belongs to 2026-07-06
    dt_night_start = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
    assert get_attendance_date(dt_night_start, "22:00", "06:00") == "2026-07-06"

    # Punch in 05:30 on 2026-07-07 (< 06:00 shift end) belongs to previous day (2026-07-06)
    dt_night_early = datetime(2026, 7, 7, 5, 30, 0, tzinfo=IST)
    assert get_attendance_date(dt_night_early, "22:00", "06:00") == "2026-07-06"

    # Punch in 06:00 on 2026-07-07 (>= 06:00 shift end) belongs to 2026-07-07
    dt_night_after = datetime(2026, 7, 7, 6, 0, 0, tzinfo=IST)
    assert get_attendance_date(dt_night_after, "22:00", "06:00") == "2026-07-07"


def test_get_shift_window():
    # Regular shift
    start_dt, end_dt = get_shift_window("2026-07-06", "09:30", "18:30")
    assert start_dt == datetime(2026, 7, 6, 9, 30, tzinfo=IST)
    assert end_dt == datetime(2026, 7, 6, 18, 30, tzinfo=IST)

    # Overnight shift: end falls on next calendar day
    start_dt, end_dt = get_shift_window("2026-07-06", "22:00", "06:00")
    assert start_dt == datetime(2026, 7, 6, 22, 0, tzinfo=IST)
    assert end_dt == datetime(2026, 7, 7, 6, 0, tzinfo=IST)


def test_compute_late_minutes_r2():
    # Shift 09:30:
    # 09:40:00 -> exactly 10 min -> 0 late
    p1 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(p1, "09:30", "18:30", "2026-07-06") == 0

    # 09:40:01 -> strictly > 10 min -> 10 late
    p2 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(p2, "09:30", "18:30", "2026-07-06") == 10

    # 10:15:59 -> 45 late
    p3 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(p3, "09:30", "18:30", "2026-07-06") == 45

    # Early punch-in 09:15 -> 0 late
    p4 = datetime(2026, 7, 6, 9, 15, 0, tzinfo=IST)
    assert compute_late_minutes(p4, "09:30", "18:30", "2026-07-06") == 0

    # Overnight shift (22:00 - 06:00) on 2026-07-06
    # 22:10:00 -> 0 late
    p5 = datetime(2026, 7, 6, 22, 10, 0, tzinfo=IST)
    assert compute_late_minutes(p5, "22:00", "06:00", "2026-07-06") == 0

    # 22:10:01 -> 10 late
    p6 = datetime(2026, 7, 6, 22, 10, 1, tzinfo=IST)
    assert compute_late_minutes(p6, "22:00", "06:00", "2026-07-06") == 10

    # Next morning punch-in at 05:30 (shift started previous day at 22:00)
    # Elapsed = 7.5 hours = 450 minutes
    p7 = datetime(2026, 7, 7, 5, 30, 0, tzinfo=IST)
    assert compute_late_minutes(p7, "22:00", "06:00") == 450


def test_compute_overtime_r3():
    # Regular shift (09:30 - 18:30) on 2026-07-06
    # 18:35:00 -> 5 min -> 0 overtime (< 30 min)
    out1 = datetime(2026, 7, 6, 18, 35, 0, tzinfo=IST)
    assert compute_overtime(out1, "18:30", "2026-07-06", "09:30") == 0

    # 18:59:59 -> 29 min -> 0 overtime (< 30 min)
    out2 = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST)
    assert compute_overtime(out2, "18:30", "2026-07-06", "09:30") == 0

    # 19:00:00 -> 30 min -> 30 overtime (>= 30 min)
    out3 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
    assert compute_overtime(out3, "18:30", "2026-07-06", "09:30") == 30

    # 19:10:00 -> 40 min -> 40 overtime (Sample 2)
    out4 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST)
    assert compute_overtime(out4, "18:30", "2026-07-06", "09:30") == 40

    # Overnight shift (22:00 - 06:00) starting 2026-07-06, ends 2026-07-07 06:00
    # Punch out 06:40:00 -> 40 overtime (Sample 7)
    out5 = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    assert compute_overtime(out5, "06:00", "2026-07-06", "22:00") == 40

    # Punch out 06:20:00 -> 20 min -> 0 overtime
    out6 = datetime(2026, 7, 7, 6, 20, 0, tzinfo=IST)
    assert compute_overtime(out6, "06:00", "2026-07-06", "22:00") == 0


def test_compute_work_hours_half_up_r4():
    # Sample 1: 03:58:00Z to 13:05:00Z -> 32820 sec -> 9.11666... -> 9.12
    p_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    p_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_work_hours(p_in, p_out) == 9.12

    # Sample 2: 04:35:00Z to 13:40:00Z -> 32700 sec -> 9.08333... -> 9.08
    p_in2 = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
    p_out2 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_work_hours(p_in2, p_out2) == 9.08

    # Half-up rounding boundary: 16182 sec / 3600 = 4.495000 -> 4.50
    p_in3 = datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
    p_out3 = datetime(2026, 7, 6, 4, 29, 42, tzinfo=timezone.utc)
    assert compute_work_hours(p_in3, p_out3) == 4.50

    # Half-up rounding boundary: 16178 sec / 3600 = 4.493888... -> 4.49
    p_out3_sub = datetime(2026, 7, 6, 4, 29, 38, tzinfo=timezone.utc)
    assert compute_work_hours(p_in3, p_out3_sub) == 4.49

    assert compute_work_hours(None, p_out) is None
    assert compute_work_hours(p_in, None) is None


def test_compute_half_day_r5():
    assert compute_half_day(3.50) is True
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(9.12) is False
    assert compute_half_day(None) is False


def test_mongo_indexes():
    init_db()

    # Check employees indexes
    emp_indexes = db.employees.index_information()
    assert "emp_code_1" in emp_indexes
    assert emp_indexes["emp_code_1"].get("unique") is True
    assert "department_1" in emp_indexes

    # Check attendance_logs indexes
    att_indexes = db.attendance_logs.index_information()
    assert "emp_code_1_date_1" in att_indexes
    assert att_indexes["emp_code_1_date_1"].get("unique") is True
    assert "date_-1_emp_code_1" in att_indexes
    assert "status_1" in att_indexes

    # Test uniqueness enforcement on emp_code
    db.employees.delete_many({"emp_code": {"$in": ["TEST_EMP_99", "TEST_EMP_99_DUP"]}})
    db.employees.insert_one({"emp_code": "TEST_EMP_99", "name": "Test", "department": "QA"})
    with pytest.raises(DuplicateKeyError):
        db.employees.insert_one({"emp_code": "TEST_EMP_99", "name": "Test Dupl", "department": "QA"})

    # Test uniqueness enforcement on (emp_code, date)
    db.attendance_logs.delete_many({"emp_code": "TEST_EMP_99"})
    db.attendance_logs.insert_one({"emp_code": "TEST_EMP_99", "date": "2026-07-06", "status": "PRESENT"})
    with pytest.raises(DuplicateKeyError):
        db.attendance_logs.insert_one({"emp_code": "TEST_EMP_99", "date": "2026-07-06", "status": "WFH"})

    # Cleanup test records
    db.employees.delete_many({"emp_code": "TEST_EMP_99"})
    db.attendance_logs.delete_many({"emp_code": "TEST_EMP_99"})
