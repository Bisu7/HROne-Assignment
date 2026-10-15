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
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Union, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import PyMongoError, DuplicateKeyError, ServerSelectionTimeoutError

load_dotenv(override=False)

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]


IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Application Lifespan: Idempotent Database Index Creation
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Creates all required indexes in MongoDB idempotently at application startup.
    
    Why each index exists:
    1. employees.emp_code (UNIQUE):
       - Enforces uniqueness of business employee codes at the database level.
       - Guarantees race-safe employee creation and fast O(1) employee lookups.
    2. employees.(department, emp_code):
       - Accelerates GET /employees filtered by department and sorted by emp_code ascending.
       - Supports covered/index scans for department-filtered queries.
    3. employees.joined_on:
       - Speeds up analytics pipelines filtering employees by joined_on (headcount rules R7, R9).
    4. attendance_logs.(emp_code, date) (UNIQUE):
       - Enforces the natural key: at most one attendance record per employee per calendar date.
       - Prevents duplicate punch-ins under concurrency without race conditions.
       - Backs fast O(1) lookups for regularizations (PATCH /attendance/{emp_code}/{date}).
    5. attendance_logs.(date, emp_code):
       - Accelerates GET /attendance default sort (date DESC, emp_code ASC) and date range filters.
    6. attendance_logs.(status, date):
       - Speeds up queries filtering by status and date range for analytics summaries.
    7. attendance_logs.(emp_code, punch_in):
       - Speeds up finding an employee's most recent open punch-in during punch-out.
    """
    try:
        # Create indexes for 'employees' collection
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_employees_emp_code_unique")
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="idx_employees_dept_emp_code")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_employees_dept_joined_on")
        db.employees.create_index([("joined_on", ASCENDING)], name="idx_employees_joined_on")

        # Create indexes for 'attendance_logs' collection
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="idx_attendance_emp_date_unique")
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_attendance_date_emp_code")
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)], name="idx_attendance_date_late_minutes")
        db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="idx_attendance_status_date")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="idx_attendance_emp_punch_in")
    except PyMongoError as exc:
        print(f"Warning: Index creation failed or deferred at startup: {exc}")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ms_to_utc_datetime(ms: int) -> datetime:
    """Converts epoch milliseconds to an aware UTC datetime, truncated to whole seconds (R1)."""
    # Taking integer floor of seconds drops fractional milliseconds as required by R1
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a datetime (naive UTC or timezone-aware) to an integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ist_date(dt: datetime) -> str:
    """Returns the IST calendar date string (YYYY-MM-DD) for a given instant."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ist_dt = dt.astimezone(IST)
    return ist_dt.strftime("%Y-%m-%d")


def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """Computes the attendance date string (YYYY-MM-DD) following rule R1.
    
    Overnight shift rule: if shift_end <= shift_start (e.g. 22:00 to 06:00),
    a punch-in strictly earlier than shift_end belongs to the shift that started
    the previous calendar day.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    ist_dt = punch_in_utc.astimezone(IST)

    # Check for overnight shift
    if shift_end <= shift_start:
        end_h, end_m = map(int, shift_end.split(":"))
        punch_minutes = ist_dt.hour * 60 + ist_dt.minute
        shift_end_minutes = end_h * 60 + end_m
        if punch_minutes < shift_end_minutes:
            # Belongs to previous day's shift
            return (ist_dt - timedelta(days=1)).strftime("%Y-%m-%d")

    return ist_dt.strftime("%Y-%m-%d")


def shift_start_instant(date_str: str, shift_start: str) -> datetime:
    """Returns the UTC instant when a shift starts on the given attendance date."""
    h, m = map(int, shift_start.split(":"))
    year, month, day = map(int, date_str.split("-"))
    ist_start = datetime(year, month, day, h, m, 0, tzinfo=IST)
    return ist_start.astimezone(timezone.utc)


def shift_end_instant(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """Returns the UTC instant when a shift ends.
    
    If overnight shift (shift_end <= shift_start), the shift ends on the next calendar day.
    """
    h, m = map(int, shift_end.split(":"))
    year, month, day = map(int, date_str.split("-"))
    ist_end = datetime(year, month, day, h, m, 0, tzinfo=IST)
    if shift_end <= shift_start:
        ist_end += timedelta(days=1)
    return ist_end.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Business Rule Helpers (R2 - R5)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in_utc: datetime, shift_start: Union[datetime, str], date_str: Optional[str] = None) -> int:
    """Computes late minutes per rule R2.
    
    Grace period is 10 minutes. A punch-in is late ONLY if it is strictly
    more than 10 minutes (600 seconds) after shift_start.
    When late, late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    Otherwise 0.
    """
    if isinstance(shift_start, str):
        d = date_str or ist_date(punch_in_utc)
        shift_start_utc = shift_start_instant(d, shift_start)
    else:
        shift_start_utc = shift_start

    diff_seconds = (punch_in_utc - shift_start_utc).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_overtime(punch_out_utc: datetime, shift_end: Union[datetime, str], date_str: Optional[str] = None, shift_start: Optional[str] = None) -> int:
    """Computes overtime minutes per rule R3.
    
    Overtime = whole minutes (floor) between shift_end and punch_out.
    Counted only when that is >= 30; otherwise 0.
    """
    if isinstance(shift_end, str):
        d = date_str or ist_date(punch_out_utc)
        shift_end_utc = shift_end_instant(d, shift_start or "09:30", shift_end)
    else:
        shift_end_utc = shift_end

    diff_seconds = (punch_out_utc - shift_end_utc).total_seconds()
    if diff_seconds < 0:
        return 0
    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """Computes work hours per rule R4.
    
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up
    using Decimal.ROUND_HALF_UP (never Python's round() which uses banker's rounding).
    """
    diff_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_seconds <= 0:
        return 0.0
    secs = Decimal(str(diff_seconds))
    hours = (secs / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """Computes half-day flag per rule R5.
    
    After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Validation Helpers
# --------------------------------------------------------------------------- #
TIME_REGEX = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
MONTH_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def is_valid_time(s: str) -> bool:
    """Validates 24-hour HH:MM format."""
    return bool(TIME_REGEX.match(s))


def is_valid_date(s: str) -> bool:
    """Validates calendar date format YYYY-MM-DD."""
    if not DATE_REGEX.match(s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def is_valid_month(s: str) -> bool:
    """Validates calendar month format YYYY-MM."""
    return bool(MONTH_REGEX.match(s))


# --------------------------------------------------------------------------- #
# Document Shape Transformers
# --------------------------------------------------------------------------- #
def doc_to_attendance_response(doc: dict) -> dict:
    """Converts a stored MongoDB attendance document to the API response shape.
    
    - Drops internal _id and avoids any 'id' field
    - Converts BSON UTC datetimes to epoch milliseconds (including history punch times)
    - Normalizes missing fields: history -> [], half_day -> false,
      late_minutes -> 0, overtime_minutes -> 0
    """
    doc.pop("_id", None)
    doc.pop("id", None)

    raw_history = doc.get("history") or []
    cleaned_history = []
    for entry in raw_history:
        at_val = entry.get("at")
        at_ms = datetime_to_ms(at_val) if isinstance(at_val, datetime) else at_val

        changes = entry.get("changes") or {}
        cleaned_changes = {}
        for field, change in changes.items():
            from_val = change.get("from")
            to_val = change.get("to")
            if field in ("punch_in", "punch_out"):
                from_val = datetime_to_ms(from_val) if isinstance(from_val, datetime) else from_val
                to_val = datetime_to_ms(to_val) if isinstance(to_val, datetime) else to_val
            cleaned_changes[field] = {"from": from_val, "to": to_val}

        cleaned_history.append({
            "at": at_ms,
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": cleaned_changes,
        })

    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")

    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": datetime_to_ms(punch_in) if isinstance(punch_in, datetime) else punch_in,
        "punch_out": datetime_to_ms(punch_out) if isinstance(punch_out, datetime) else punch_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0) if doc.get("late_minutes") is not None else 0,
        "overtime_minutes": doc.get("overtime_minutes", 0) if doc.get("overtime_minutes") is not None else 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": cleaned_history,
    }


def doc_to_employee_response(doc: dict) -> dict:
    """Converts a stored MongoDB employee document to the API response shape."""
    doc.pop("_id", None)
    doc.pop("id", None)
    created_at = doc.get("created_at")
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": datetime_to_ms(created_at) if isinstance(created_at, datetime) else created_at,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., min_length=7, max_length=9)
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30")
    shift_end: str = Field("18:30")
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not re.match(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("Invalid email format")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_times(cls, v: str) -> str:
        if not is_valid_time(v):
            raise ValueError("Shift times must be HH:MM in 24-hour format")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        if not is_valid_date(v):
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return v

    @model_validator(mode="after")
    def validate_shift_difference(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided (Fixed)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        # Ping MongoDB admin database to verify real database connectivity
        client.admin.command("ping")
        return {"status": "ok"}
    except (PyMongoError, ServerSelectionTimeoutError, Exception):
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    # Server generates created_at as an aware UTC datetime, truncated to whole seconds (R1)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        # Relies on the unique index to catch race conditions reliably
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return doc_to_employee_response(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q)
        .sort([("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [doc_to_employee_response(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # If punched_at is given, convert to UTC truncated to whole seconds; otherwise use now
    if body.punched_at is not None:
        punch_in_utc = ms_to_utc_datetime(body.punched_at)
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Determine attendance date according to R1 (including overnight shift rule)
    att_date = attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])

    # Compute late minutes according to R2
    late_minutes = compute_late_minutes(punch_in_utc, emp["shift_start"], date_str=att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        # Unique index on (emp_code, date) ensures atomic rejection of duplicate punches
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return doc_to_attendance_response(doc)


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
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
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None and not is_valid_date(date_from):
        raise HTTPException(status_code=422, detail="Invalid date_from format")
    if date_to is not None and not is_valid_date(date_to):
        raise HTTPException(status_code=422, detail="Invalid date_to format")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
    if status is not None and status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
        raise HTTPException(status_code=422, detail="Invalid status value")

    q = build_attendance_list_query(emp_code, date_from, date_to, status)

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [doc_to_attendance_response(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Endpoints: Punch-Out & Regularize
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # If punched_at is given, convert to UTC truncated to whole seconds; otherwise use now
    if body.punched_at is not None:
        punch_out_utc = ms_to_utc_datetime(body.punched_at)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the employee's most recent record whose punch_in is at or before punched_at
    # This works across midnight for overnight shifts
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_seconds <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
    if diff_seconds > 86400:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Compute derived fields R3, R4, R5
    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    shift_end_utc = shift_end_instant(record["date"], emp["shift_start"], emp["shift_end"])
    overtime_minutes = compute_overtime(punch_out_utc, shift_end_utc)
    half_day = compute_half_day(work_hours)

    # Atomic update on punch_out: null to eliminate concurrent punch-out race conditions
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        # A concurrent request already closed this record
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return doc_to_attendance_response(updated_doc)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    if not is_valid_date(date):
        raise HTTPException(status_code=422, detail="Invalid date format in path")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Optimistic locking retry loop (up to 5 attempts) to prevent lost updates under concurrency
    for attempt in range(5):
        record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if not record:
            raise HTTPException(status_code=404, detail="Attendance record not found")

        current_status = record.get("status")
        current_punch_in = record.get("punch_in")
        current_punch_out = record.get("punch_out")
        current_work_hours = record.get("work_hours")
        current_late_minutes = record.get("late_minutes", 0)
        current_overtime_minutes = record.get("overtime_minutes", 0)
        current_half_day = bool(record.get("half_day", False))

        target_status = body.status if body.status is not None else current_status

        # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch times is 422
        if target_status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(
                    status_code=422,
                    detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE",
                )
            target_punch_in = None
            target_punch_out = None
            new_work_hours = None
            new_late_minutes = 0
            new_overtime_minutes = 0
            new_half_day = False
        else:
            # Presence status requires punch_in
            if body.punch_in is not None:
                target_punch_in = ms_to_utc_datetime(body.punch_in)
            else:
                target_punch_in = current_punch_in

            if target_punch_in is None:
                raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

            if target_punch_in.tzinfo is None:
                target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

            # Rule R1: punch_in must stay on the record's attendance date
            calc_date = attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
            if calc_date != date:
                raise HTTPException(
                    status_code=422,
                    detail=f"punch_in falls on date {calc_date}, which does not match record date {date}",
                )

            # Punch out
            if body.punch_out is not None:
                target_punch_out = ms_to_utc_datetime(body.punch_out)
            else:
                target_punch_out = current_punch_out

            if target_punch_out is not None:
                if target_punch_out.tzinfo is None:
                    target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)
                diff_seconds = (target_punch_out - target_punch_in).total_seconds()
                if diff_seconds <= 0:
                    raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
                if diff_seconds > 86400:
                    raise HTTPException(
                        status_code=422,
                        detail="punch_out cannot be more than 24 hours after punch_in",
                    )
                new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
                shift_end_utc = shift_end_instant(date, emp["shift_start"], emp["shift_end"])
                new_overtime_minutes = compute_overtime(target_punch_out, shift_end_utc)
                new_half_day = compute_half_day(new_work_hours)
            else:
                new_work_hours = None
                new_overtime_minutes = 0
                new_half_day = False

            new_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date_str=date)

        # Build changes dictionary containing only fields that actually changed
        changes = {}

        if target_status != current_status:
            changes["status"] = {"from": current_status, "to": target_status}

        if target_punch_in != current_punch_in:
            changes["punch_in"] = {"from": current_punch_in, "to": target_punch_in}

        if target_punch_out != current_punch_out:
            changes["punch_out"] = {"from": current_punch_out, "to": target_punch_out}

        if new_work_hours != current_work_hours:
            changes["work_hours"] = {"from": current_work_hours, "to": new_work_hours}

        if new_late_minutes != current_late_minutes:
            changes["late_minutes"] = {"from": current_late_minutes, "to": new_late_minutes}

        if new_overtime_minutes != current_overtime_minutes:
            changes["overtime_minutes"] = {"from": current_overtime_minutes, "to": new_overtime_minutes}

        if new_half_day != current_half_day:
            changes["half_day"] = {"from": current_half_day, "to": new_half_day}

        # Rule: A request that changes nothing is 422
        if not changes:
            raise HTTPException(status_code=422, detail="Request resulted in no changes to the record")

        # Create history entry
        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        history_entry = {
            "at": now_utc,
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }

        # Atomic update with optimistic concurrency control on the fields read
        updated_doc = db.attendance_logs.find_one_and_update(
            {
                "emp_code": emp_code,
                "date": date,
                "status": current_status,
                "punch_in": current_punch_in,
                "punch_out": current_punch_out,
            },
            {
                "$set": {
                    "status": target_status,
                    "punch_in": target_punch_in,
                    "punch_out": target_punch_out,
                    "work_hours": new_work_hours,
                    "late_minutes": new_late_minutes,
                    "overtime_minutes": new_overtime_minutes,
                    "half_day": new_half_day,
                },
                "$push": {
                    "history": history_entry
                },
            },
            return_document=ReturnDocument.AFTER,
        )

        if updated_doc:
            return doc_to_attendance_response(updated_doc)

    # Failed after retries due to concurrent modification
    raise HTTPException(status_code=409, detail="Concurrent regularization conflict; please retry")


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    """Builds the single aggregation pipeline for employee monthly analytics.
    
    Index used: idx_attendance_emp_date_unique on attendance_logs (emp_code, date).
    The $match stage performs an IXSCAN using emp_code and date bounds.
    """
    return [
        # Stage 1: Match employee and month range using compound index (emp_code, date)
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": f"{month}-01",
                    "$lte": f"{month}-31",
                },
            }
        },
        # Stage 2: Group and accumulate monthly metrics
        {
            "$group": {
                "_id": None,
                # present_days: Mon-Fri only ($dayOfWeek 2..6), half_day counts as 0.5 (R5, R6, R7)
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                # leave_days: count of LEAVE records
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                # late_count: records with late_minutes > 0 (includes weekend records per R7)
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                # total_late_minutes: sum of late_minutes (includes weekends per R7)
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                # total_overtime_minutes: sum of overtime_minutes (includes weekends per R7)
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    if not is_valid_month(month):
        raise HTTPException(status_code=422, detail="Invalid month format, expected YYYY-MM")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Run MongoDB aggregation pipeline
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_result = list(db.attendance_logs.aggregate(pipeline))

    if agg_result:
        row = agg_result[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        # Default zero counts if employee has zero logs for the month
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    # Calculate working days: Mon-Fri in the month from joined_on for mid-month joiners (R7)
    year, month_num = map(int, month.split("-"))
    _, days_in_month = calendar.monthrange(year, month_num)
    month_start = f"{month}-01"
    month_end = f"{month}-{days_in_month:02d}"

    joined_on = emp.get("joined_on", "1970-01-01")
    calc_start_date_str = max(month_start, joined_on)

    working_days = 0
    if calc_start_date_str <= month_end:
        cur_date = datetime.strptime(calc_start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(month_end, "%Y-%m-%d").date()
        while cur_date <= end_date:
            if cur_date.weekday() < 5:  # Monday through Friday
                working_days += 1
            cur_date += timedelta(days=1)

    # Attendance percentage: present_days / working_days * 100 rounded to 2 decimals half-up (R8)
    # null when working_days is 0
    if working_days > 0:
        pct_dec = (Decimal(str(present_days)) / Decimal(str(working_days)) * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        attendance_pct = float(pct_dec)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# Rule R8 Rounding Helper
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[Union[float, Decimal]], decimals: int = 2) -> Optional[float]:
    """Rounds a number half-up per rule R8 using Decimal.ROUND_HALF_UP."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("0." + "0" * (decimals - 1) + "1") if decimals > 0 else Decimal("1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Pipeline Builders & Endpoints: Department Summary, Leaderboard & Trend
# --------------------------------------------------------------------------- #
def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    """Builds the aggregation pipeline for department summary.
    
    Index used: employees.(joined_on) and employees.(department, emp_code).
    Starts from employees so people with zero logs are included in headcount (R9),
    and people who joined after month end are excluded.
    """
    year, month_num = map(int, month.split("-"))
    _, days_in_month = calendar.monthrange(year, month_num)
    month_end = f"{month}-{days_in_month:02d}"

    match_filter: dict = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_filter["department"] = department

    return [
        # Stage 1: Filter employees who joined on or before month end (R9)
        {"$match": match_filter},
        # Stage 2: Lookup attendance logs for this employee in the month
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp_code"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", f"{month}-31"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        # Stage 3: Reduce logs per employee
        {
            "$project": {
                "department": 1,
                # present_days: Mon-Fri only, half_day counts as 0.5 (R5, R6, R7)
                "emp_present_days": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$$this.date"
                                                                }
                                                            }
                                                        },
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                                {
                                                    "$in": [
                                                        "$$this.status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                            ]
                                        },
                                        {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "emp_late_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "emp_total_late_minutes": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$ifNull": ["$$this.late_minutes", 0]},
                            ]
                        },
                    }
                },
                "emp_leave_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]},
                            ]
                        },
                    }
                },
                "emp_on_duty_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]},
                            ]
                        },
                    }
                },
                # For avg_work_hours over records (not over employee averages)
                "emp_work_hours_sum": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$$this.work_hours", None]},
                                            ]
                                        },
                                        "$$this.work_hours",
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "emp_work_hours_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$$this.work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                },
                            ]
                        },
                    }
                },
            }
        },
        # Stage 4: Group by department across all employees
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "total_work_hours": {"$sum": "$emp_work_hours_sum"},
                "total_work_hours_count": {"$sum": "$emp_work_hours_count"},
            }
        },
        # Stage 5: Project final metrics; average is over records
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$total_work_hours_count", 0]},
                        {"$divide": ["$total_work_hours", "$total_work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        # Stage 6: Sort by department ascending
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    if not is_valid_month(month):
        raise HTTPException(status_code=422, detail="Invalid month format, expected YYYY-MM")

    pipeline = build_department_summary_pipeline(month, department)
    raw_items = list(db.employees.aggregate(pipeline))

    items = []
    for r in raw_items:
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r.get("present_days"), 2) or 0.0,
            "avg_work_hours": round_half_up(r.get("avg_work_hours"), 2),
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list[dict]:
    """Builds the aggregation pipeline for late-comers leaderboard.
    
    Index used: idx_attendance_date_emp_code or idx_attendance_status_date on attendance_logs.
    Uses $setWindowFields with $rank for standard competition ranking (1, 2, 2, 4),
    applying limit after ranking.
    """
    pipeline = [
        # Stage 1: Match attendance logs with late_minutes > 0 in the month
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
                "late_minutes": {"$gt": 0},
            }
        },
        # Stage 2: Group by employee to aggregate late minutes and count
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # Stage 3: Lookup employee details (name, department)
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        # Stage 4: Unwind employee (ignores logs if employee does not exist)
        {"$unwind": "$emp"},
    ]

    # Stage 5: Department filter if provided (ranking happens inside department)
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    # Stage 6: Competition ranking via $setWindowFields and $rank (1, 2, 2, 4)
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        # Stage 7: Filter rank <= limit AFTER ranking
        {"$match": {"rank": {"$lte": limit}}},
        # Stage 8: Sort tied rows by emp_code ascending
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        # Stage 9: Project final schema
        {
            "$project": {
                "_id": 0,
                "rank": "$rank",
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
    ])

    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    if not is_valid_month(month):
        raise HTTPException(status_code=422, detail="Invalid month format, expected YYYY-MM")

    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list[dict]:
    """Builds the aggregation pipeline for department daily trend with gap-filling.
    
    Index used: idx_employees_dept_emp_code on employees.
    Generates the day series inside MongoDB using $range and $dateAdd,
    and computes the 7-day moving average using $setWindowFields with documents [-6, 0].
    """
    from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
    to_dt = datetime.strptime(to_date, "%Y-%m-%d").date()
    num_days = (to_dt - from_dt).days + 1

    return [
        # Stage 1: Match 1 employee in department using idx_employees_dept_emp_code
        {"$match": {"department": department}},
        {"$limit": 1},
        # Stage 2: Generate day offsets 0..num_days using $range
        {"$project": {"day_offset": {"$range": [0, num_days]}}},
        {"$unwind": "$day_offset"},
        # Stage 3: Add day_offset to from_date using $dateAdd
        {
            "$project": {
                "date_dt": {
                    "$dateAdd": {
                        "startDate": {"$dateFromString": {"dateString": from_date}},
                        "unit": "day",
                        "amount": "$day_offset",
                    }
                }
            }
        },
        # Stage 4: Date string and weekday check (Mon-Fri)
        {
            "$project": {
                "date": {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d"}},
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date_dt"},
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        # Stage 5: Count headcount (department employees joined on or before this day, R9)
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_info",
            }
        },
        # Stage 6: Lookup attendance logs for this department on this day
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$cur_day"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {
                        "$match": {
                            "emp.department": department
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "logs_summary",
            }
        },
        # Stage 7: Project base day counts
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": {"$ifNull": [{"$first": "$hc_info.count"}, 0]},
                "present_count": {"$ifNull": [{"$first": "$logs_summary.present_count"}, 0.0]},
                "late_count": {"$ifNull": [{"$first": "$logs_summary.late_count"}, 0]},
            }
        },
        # Stage 8: Compute attendance_rate (null on weekends or headcount 0; present_count / headcount on working days)
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        # Stage 9: Compute moving_avg_7d using $setWindowFields with documents [-6, 0]
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        # Stage 10: Sort by date ascending
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    if not is_valid_date(from_date):
        raise HTTPException(status_code=422, detail="Invalid from date format, expected YYYY-MM-DD")
    if not is_valid_date(to_date):
        raise HTTPException(status_code=422, detail="Invalid to date format, expected YYYY-MM-DD")

    from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
    to_dt = datetime.strptime(to_date, "%Y-%m-%d").date()

    if to_dt < from_dt:
        raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")

    num_days = (to_dt - from_dt).days + 1
    if num_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # 404 for unknown department (no employees)
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    raw_items = list(db.employees.aggregate(pipeline))

    items = []
    for r in raw_items:
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(r.get("present_count"), 2) or 0.0,
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(r.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(r.get("moving_avg_7d"), 4),
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain Endpoint
# --------------------------------------------------------------------------- #
VALID_EXPLAIN_ENDPOINTS = {
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in VALID_EXPLAIN_ENDPOINTS:
        raise HTTPException(status_code=404, detail="Unknown explain endpoint")

    if endpoint == "attendance_list":
        if date_from is not None and not is_valid_date(date_from):
            raise HTTPException(status_code=422, detail="Invalid date_from format")
        if date_to is not None and not is_valid_date(date_to):
            raise HTTPException(status_code=422, detail="Invalid date_to format")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
        if status is not None and status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(status_code=422, detail="Invalid status value")

        collection = "attendance_logs"
        q = build_attendance_list_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        cmd = {
            "find": collection,
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain_output = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="Missing required query parameter: emp_code")
        if not month or not is_valid_month(month):
            raise HTTPException(status_code=422, detail="Missing or invalid query parameter: month (YYYY-MM)")

        collection = "attendance_logs"
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        cmd = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_output = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "department_summary":
        if not month or not is_valid_month(month):
            raise HTTPException(status_code=422, detail="Missing or invalid query parameter: month (YYYY-MM)")

        collection = "employees"
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_output = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        if not month or not is_valid_month(month):
            raise HTTPException(status_code=422, detail="Missing or invalid query parameter: month (YYYY-MM)")

        collection = "attendance_logs"
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        cmd = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_output = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="Missing required query parameter: department")
        if not from_date or not is_valid_date(from_date):
            raise HTTPException(status_code=422, detail="Missing or invalid query parameter: from (YYYY-MM-DD)")
        if not to_date or not is_valid_date(to_date):
            raise HTTPException(status_code=422, detail="Missing or invalid query parameter: to (YYYY-MM-DD)")

        from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_dt = datetime.strptime(to_date, "%Y-%m-%d").date()
        if to_dt < from_dt:
            raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")
        if (to_dt - from_dt).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        collection = "employees"
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        cmd = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_output = db.command("explain", cmd, verbosity="executionStats")

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain_output,
    }
