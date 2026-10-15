# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service implementing employee attendance tracking, regularization, and department analytics compliant with `openapi.yaml`.

---

## Prerequisites

- **Python**: 3.11 or newer
- **MongoDB**: 6.0 or newer (tested with MongoDB Atlas M0 cluster)

---

## Setup & Running

### 1. Environment Setup
Create and activate a virtual environment:
```bash
python -m venv .venv

# On Linux/macOS:
source .venv/bin/activate

# On Windows:
.venv\Scripts\activate
```

Install dependencies:
```bash
pip install -r requirements.txt
```

### 2. Environment Variables
Create a `.env` file (or set environment variables in your shell):
```env
MONGO_URI=mongodb+srv://<username>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority
MONGO_DB=HROne-Assignment
```

### 3. Seed Sample Data
Load initial seed data:
```bash
python sample_seed.py
```

### 4. Start the Application
```bash
uvicorn app.main:app --port 8000
```
Interactive API docs will be available at: `http://localhost:8000/docs`.

---

## Running Local Verification Tests

Run the edge-case test suite covering business rule boundaries (R1–R10), validations, race conditions (20 parallel punch-ins), and aggregation accuracy:
```bash
python local_test.py
```

---

## Design Highlights

1. **Single File Architecture**: All logic resides cleanly in `app/main.py`.
2. **Race Safety**:
   - Punch-in concurrency is enforced at the database level by a unique compound index on `(emp_code, date)`.
   - Punch-out concurrency is handled via atomic `find_one_and_update` on `{emp_code, date, punch_out: None}`.
   - Regularization updates use optimistic locking with automatic retries.
3. **Pure MongoDB Analytics**: Monthly employee summaries, department rollups, leaderboard ranking, and trend analysis are executed entirely within MongoDB aggregation pipelines using index scans (`IXSCAN`).
4. **Rounding & Precision**: Uses Python's `decimal` module with `ROUND_HALF_UP` to strictly avoid banker's rounding discrepancies.
5. **Zero 500s**: Comprehensive schema validation using Pydantic ensures clean 422 or 404 responses for malformed inputs or missing entities.
