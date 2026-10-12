"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
admin_db = client.admin

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Database Initialization & Indexes
# --------------------------------------------------------------------------- #
def init_db():
    # employees indexes:
    # 1. Unique index on emp_code (enforced per API contract)
    db.employees.create_index([("emp_code", 1)], unique=True)
    # 2. Index on department for filtering by department
    db.employees.create_index([("department", 1)])
    # 3. Index on joined_on for department_summary / department_trend headcount
    #    queries that filter employees by joined_on <= period_end.
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs indexes:
    # 1. Unique compound index on (emp_code, date) – enforces one record per
    #    employee per day and is the primary seek path for employee_monthly.
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    # 2. Compound index for default sort (date desc, emp_code asc) – covers
    #    attendance_list bare sort and date-range scans in late_leaderboard /
    #    department_trend.  MongoDB walks this index and returns results in sort
    #    order without an additional blocking SORT stage.
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    # 3. Status index for filtering by status alone
    db.attendance_logs.create_index([("status", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# Ensure indexes exist on module import as well
try:
    init_db()
except Exception:
    pass


# --------------------------------------------------------------------------- #
# Core Utilities & Business Rule Helpers (R1 - R5)
# --------------------------------------------------------------------------- #
def truncate_to_seconds(ts_ms: int) -> int:
    """Truncates epoch milliseconds to whole seconds (R1)."""
    return (ts_ms // 1000) * 1000


def epoch_ms_to_datetime(ts_ms: int) -> datetime:
    """Converts epoch milliseconds to a UTC datetime truncated to whole seconds."""
    truncated_ms = truncate_to_seconds(ts_ms)
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a BSON or Python datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Returns True if shift ends on or before shift start (e.g. 22:00 - 06:00)."""
    return shift_end <= shift_start


def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in.
    Overnight shifts: a punch-in earlier than shift_end belongs to the shift
    that started the previous calendar day.
    """
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    punch_in_ist = punch_in.astimezone(IST)

    if is_overnight_shift(shift_start, shift_end):
        end_h, end_m = map(int, shift_end.split(":"))
        punch_time = (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second)
        if punch_time < (end_h, end_m, 0):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()

    return punch_in_ist.date().isoformat()


def get_shift_window(date_str: str, shift_start: str, shift_end: str) -> Tuple[datetime, datetime]:
    """
    Returns (shift_start_dt, shift_end_dt) in IST for an attendance date.
    For overnight shifts, shift_end falls on the next calendar day (R1).
    """
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    d = datetime.fromisoformat(date_str).date()
    start_dt = datetime(d.year, d.month, d.day, start_h, start_m, 0, 0, tzinfo=IST)

    if is_overnight_shift(shift_start, shift_end):
        next_d = d + timedelta(days=1)
        end_dt = datetime(next_d.year, next_d.month, next_d.day, end_h, end_m, 0, 0, tzinfo=IST)
    else:
        end_dt = datetime(d.year, d.month, d.day, end_h, end_m, 0, 0, tzinfo=IST)

    return start_dt, end_dt


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: Optional[str] = None,
    date_str: Optional[str] = None,
) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start.
    """
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    punch_in_ist = punch_in.astimezone(IST).replace(microsecond=0)

    if date_str is None:
        if shift_end is not None:
            date_str = get_attendance_date(punch_in_ist, shift_start, shift_end)
        else:
            date_str = punch_in_ist.date().isoformat()

    start_h, start_m = map(int, shift_start.split(":"))
    d = datetime.fromisoformat(date_str).date()
    shift_start_dt = datetime(d.year, d.month, d.day, start_h, start_m, 0, 0, tzinfo=IST)

    diff_sec = int((punch_in_ist - shift_start_dt).total_seconds())
    if diff_sec > 600:  # strictly more than 10 minutes (600s)
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    Null until punch-out.
    """
    if not punch_in or not punch_out:
        return None
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)

    p_in = punch_in.replace(microsecond=0)
    p_out = punch_out.replace(microsecond=0)
    diff_sec = Decimal(str(int((p_out - p_in).total_seconds())))
    hours = diff_sec / Decimal("3600")
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: str = "09:30",
) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    """
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)
    punch_out_ist = punch_out.astimezone(IST).replace(microsecond=0)

    _, shift_end_dt = get_shift_window(date_str, shift_start, shift_end)
    diff_sec = int((punch_out_ist - shift_end_dt).total_seconds())
    minutes = diff_sec // 60
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeIn(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Response Document Formatters (no MongoDB _id, convert datetimes to epoch ms)
# --------------------------------------------------------------------------- #
def format_employee_doc(e: dict) -> dict:
    e.pop("_id", None)
    e.pop("id", None)
    e["created_at"] = datetime_to_epoch_ms(e.get("created_at"))
    return e


def format_attendance_doc(d: dict) -> dict:
    d.pop("_id", None)
    d.pop("id", None)
    d["punch_in"] = datetime_to_epoch_ms(d.get("punch_in"))
    d["punch_out"] = datetime_to_epoch_ms(d.get("punch_out"))

    if "half_day" not in d or d["half_day"] is None:
        d["half_day"] = False
    if "late_minutes" not in d or d["late_minutes"] is None:
        d["late_minutes"] = 0
    if "overtime_minutes" not in d or d["overtime_minutes"] is None:
        d["overtime_minutes"] = 0
    if "work_hours" not in d:
        d["work_hours"] = None

    if "history" not in d or d["history"] is None:
        d["history"] = []
    else:
        formatted_history = []
        for h in d["history"]:
            entry = dict(h)
            entry["at"] = datetime_to_epoch_ms(entry.get("at"))
            if "changes" in entry and isinstance(entry["changes"], dict):
                formatted_changes = {}
                for field, ch in entry["changes"].items():
                    if isinstance(ch, dict):
                        from_val = ch.get("from")
                        to_val = ch.get("to")
                        if isinstance(from_val, datetime):
                            from_val = datetime_to_epoch_ms(from_val)
                        if isinstance(to_val, datetime):
                            to_val = datetime_to_epoch_ms(to_val)
                        formatted_changes[field] = {"from": from_val, "to": to_val}
                    else:
                        formatted_changes[field] = ch
                entry["changes"] = formatted_changes
            formatted_history.append(entry)
        d["history"] = formatted_history

    return d


# --------------------------------------------------------------------------- #
# Endpoints provided (Fixed)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        admin_db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        punched_ms = truncate_to_seconds(body.punched_at)
        ts = datetime.fromtimestamp(punched_ms / 1000, tz=timezone.utc)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    d = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")

    late_min = compute_late_minutes(ts, emp["shift_start"], emp["shift_end"], date_str=d)

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return format_attendance_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        punched_ms = truncate_to_seconds(body.punched_at)
        punch_out_dt = datetime.fromtimestamp(punched_ms / 1000, tz=timezone.utc)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the employee's most recent record with a punch-in
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", -1)],
    )

    if not rec:
        raise HTTPException(404, "no punch-in found")

    punch_in_dt = rec["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punched_at must be strictly after punch_in")

    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    ot = compute_overtime(punch_out_dt, emp["shift_end"], rec["date"], emp["shift_start"])
    half_day = compute_half_day(work_hours)

    # Atomic update to avoid race conditions: only update if punch_out is still None
    updated_rec = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": ot,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_rec:
        raise HTTPException(409, "record is already punched out")

    return format_attendance_doc(updated_rec)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "invalid date format, must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "attendance record not found for date")

    # Target status
    new_status = body.status if body.status is not None else rec["status"]

    # Validation: setting status ABSENT or LEAVE clears both punch times
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "cannot supply punch_in or punch_out when status is ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
    else:
        # Presence status requires punch_in (supplied or already stored)
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_datetime(body.punch_in)
        else:
            final_punch_in = rec.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(422, "presence status requires punch_in")

        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_datetime(body.punch_out)
        else:
            final_punch_out = rec.get("punch_out")

        # punch_in must stay on the record's attendance date (R1)
        expected_date = get_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if expected_date != date:
            raise HTTPException(
                422,
                f"punch_in must stay on record's attendance date ({date}), but evaluates to {expected_date}",
            )

        # punch_out must be after punch_in and within 24 hours
        if final_punch_out is not None:
            if final_punch_in.tzinfo is None:
                final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)
            if final_punch_out.tzinfo is None:
                final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)

            if final_punch_out <= final_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (final_punch_out - final_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(
            final_punch_in, emp["shift_start"], emp["shift_end"], date_str=date
        )
        if final_punch_out is not None:
            new_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            new_overtime_minutes = compute_overtime(
                final_punch_out, emp["shift_end"], date, emp["shift_start"]
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Detect what actually changed
    changes = {}

    if rec.get("status") != new_status:
        changes["status"] = {"from": rec.get("status"), "to": new_status}

    old_punch_in = rec.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    if final_punch_in is not None and final_punch_in.tzinfo is None:
        final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)
    if old_punch_in != final_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": final_punch_in}

    old_punch_out = rec.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)
    if final_punch_out is not None and final_punch_out.tzinfo is None:
        final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)
    if old_punch_out != final_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": final_punch_out}

    if rec.get("work_hours") != new_work_hours:
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": new_work_hours}

    old_late = rec.get("late_minutes", 0)
    if old_late != new_late_minutes:
        changes["late_minutes"] = {"from": old_late, "to": new_late_minutes}

    old_ot = rec.get("overtime_minutes", 0)
    if old_ot != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_overtime_minutes}

    old_half = rec.get("half_day", False)
    if old_half != new_half_day:
        changes["half_day"] = {"from": old_half, "to": new_half_day}

    if not changes:
        raise HTTPException(422, "no changes detected")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Concurrency protection: match current history state
    old_history = rec.get("history")
    if old_history is None:
        match_condition = {"_id": rec["_id"], "$or": [{"history": {"$exists": False}}, {"history": []}]}
    else:
        match_condition = {"_id": rec["_id"], "history": old_history}

    updated_rec = db.attendance_logs.find_one_and_update(
        match_condition,
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_rec:
        raise HTTPException(409, "concurrent modification conflict")

    return format_attendance_doc(updated_rec)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #
def get_month_boundaries(month: str) -> Tuple[str, str]:
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    return f"{month}-01", f"{month}-{last_day:02d}"


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    month_start, month_end = get_month_boundaries(month)

    # R7: Working days Monday - Friday, from joined_on for mid-month joiners
    start_dt = max(
        datetime.strptime(month_start, "%Y-%m-%d").date(),
        datetime.strptime(emp["joined_on"], "%Y-%m-%d").date(),
    )
    end_dt = datetime.strptime(month_end, "%Y-%m-%d").date()

    working_days = 0
    if start_dt <= end_dt:
        cur = start_dt
        while cur <= end_dt:
            if cur.weekday() < 5:  # Mon - Fri
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },
                "total_overtime_minutes": {
                    "$sum": "$overtime_minutes"
                },
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        pct_dec = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100")
        attendance_pct = float(pct_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_start, month_end = get_month_boundaries(month)

    match_stage = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code",
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 1]},
                                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 7]},
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))
    for it in items:
        if it.get("avg_work_hours") is not None:
            dec_avg = Decimal(str(it["avg_work_hours"]))
            it["avg_work_hours"] = float(dec_avg.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, month_end = get_month_boundaries(month)

    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    # Check if department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    d_from = datetime.strptime(from_date, "%Y-%m-%d").date()
    d_to = datetime.strptime(to_date, "%Y-%m-%d").date()

    if d_to < d_from:
        raise HTTPException(422, "to date must be on or after from date")

    days_diff = (d_to - d_from).days + 1
    if days_diff > 92:
        raise HTTPException(422, "date range exceeds 92 days")

    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    to_dt_bound = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {"$project": {"date": "$_id", "present_count": 1, "late_count": 1, "_id": 0}},
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {"$project": {"_id": 0, "date": {"$literal": from_date}, "dummy": {"$literal": True}}},
                ],
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": {"$ifNull": ["$present_count", 0.0]}},
                "late_count": {"$sum": {"$ifNull": ["$late_count", 0]}},
            }
        },
        {
            "$project": {
                "date": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
                "_id": 0,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_bound],
                },
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$sort": {"date": 1}},
        {
            "$addFields": {
                "is_working_day": {
                    "$and": [
                        {"$gt": [{"$dayOfWeek": "$date_dt"}, 1]},
                        {"$lt": [{"$dayOfWeek": "$date_dt"}, 7]},
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_arr",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_arr.count", 0]}, 0]}
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": "$moving_avg_7d_raw",
            }
        },
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    for it in items:
        if it.get("attendance_rate") is not None:
            dec_rate = Decimal(str(it["attendance_rate"]))
            it["attendance_rate"] = float(dec_rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        if it.get("moving_avg_7d") is not None:
            dec_ma = Decimal(str(it["moving_avg_7d"]))
            it["moving_avg_7d"] = float(dec_ma.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #

# Allowlisted endpoint names (exactly as defined in openapi.yaml enum)
_EXPLAIN_ENDPOINTS = frozenset(
    ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
)


def _bson_clean(obj):
    """Recursively convert non-JSON-serialisable BSON types to serialisable ones."""
    if isinstance(obj, dict):
        return {k: _bson_clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bson_clean(i) for i in obj]
    # ObjectId, Decimal128, etc.
    try:
        from bson import ObjectId, Decimal128, Int64
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, Decimal128):
            return float(obj.to_decimal())
        if isinstance(obj, Int64):
            return int(obj)
    except ImportError:
        pass
    return obj


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    # attendance_list params
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    # employee_monthly / department params
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    # department_trend params
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    # Validate endpoint name against allowlist (no arbitrary pipeline injection)
    if endpoint not in _EXPLAIN_ENDPOINTS:
        raise HTTPException(
            422,
            f"endpoint must be one of: {sorted(_EXPLAIN_ENDPOINTS)}",
        )

    # ------------------------------------------------------------------ #
    # attendance_list  – explain with executionStats verbosity
    # ------------------------------------------------------------------ #
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be greater than date_to")

        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        skip = (page - 1) * page_size
        # Use db.command() so we can request executionStats verbosity explicitly.
        # PyMongo's cursor.explain() does not accept a verbosity argument (uses
        # allPlansExecution by default); aggregate(explain=True) uses queryPlanner.
        explain_doc = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": _bson_clean(explain_doc),
        }

    # ------------------------------------------------------------------ #
    # employee_monthly  – main query is the $match on (emp_code, date)
    # ------------------------------------------------------------------ #
    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(422, "month is required for employee_monthly")

        month_start, month_end = get_month_boundaries(month)

        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {
                "$group": {
                    "_id": "$emp_code",
                    "present_days": {"$sum": 1},
                }
            },
        ]

        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": _bson_clean(explain_doc),
        }

    # ------------------------------------------------------------------ #
    # department_summary  – main query starts on employees
    # ------------------------------------------------------------------ #
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")

        month_start, month_end = get_month_boundaries(month)

        match_stage: dict = {"joined_on": {"$lte": month_end}}
        if department:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {"$limit": 1},
        ]

        explain_doc = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": _bson_clean(explain_doc),
        }

    # ------------------------------------------------------------------ #
    # late_leaderboard  – initial $match on (date, late_minutes)
    # ------------------------------------------------------------------ #
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")

        month_start, month_end = get_month_boundaries(month)

        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {"$limit": 1},
        ]

        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": _bson_clean(explain_doc),
        }

    # ------------------------------------------------------------------ #
    # department_trend  – initial $match on date range in attendance_logs
    # ------------------------------------------------------------------ #
    if endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        if not from_date:
            raise HTTPException(422, "from is required for department_trend")
        if not to_date:
            raise HTTPException(422, "to is required for department_trend")

        d_from = datetime.strptime(from_date, "%Y-%m-%d").date()
        d_to = datetime.strptime(to_date, "%Y-%m-%d").date()

        if d_to < d_from:
            raise HTTPException(422, "to date must be on or after from date")

        days_diff = (d_to - d_from).days + 1
        if days_diff > 92:
            raise HTTPException(422, "date range exceeds 92 days")

        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$limit": 1},
        ]

        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": _bson_clean(explain_doc),
        }

