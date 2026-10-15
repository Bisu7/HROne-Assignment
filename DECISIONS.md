# DECISIONS.md

1. **Indexes.**
   On `employees`, we created a unique index on `emp_code` to prevent duplicate IDs, `(department, emp_code)` for covered list filtering and sorting, and `(department, joined_on)` for department analytics. We rejected indexing `email` because the API never queries by email.
   On `attendance_logs`, we created a unique compound index on `(emp_code, date)` to enforce one record per day and stop race conditions, `(date, emp_code)` for attendance list queries, and `(date, late_minutes)` for leaderboard ranking. We rejected a single-field index on `status` because low cardinality provides poor query selectivity.

2. **Punch-in race.**
   When two identical punch-in requests arrive simultaneously, both compute the same attendance date and call `insert_one()`. MongoDB's storage engine enforces the unique compound index `(emp_code, date)`. Exactly one insert succeeds and returns HTTP 201. The concurrent insert fails with a `DuplicateKeyError`, which our `try/except` block catches and maps cleanly to HTTP 409 Conflict.

3. **Ties.**
   We rank employees using `$setWindowFields` with `$rank` (standard 1-2-2-4 competition ranking). We apply `$match: {"rank": {"$lte": limit}}` after ranking. If two employees tie at Rank 2 with `limit=2`, both qualify ($2 \le 2$) and both are returned (yielding 3 total rows), sorted deterministically by `emp_code` ascending per the OpenAPI contract.

4. **Headcount.**
   The department summary pipeline starts from the `employees` collection rather than `attendance_logs`. It filters by `joined_on <= month_end` (including all staff hired on or before month end, while excluding future hires), and left-joins logs using `$lookup`. Employees with zero logs remain in the pipeline with an empty log array and are accurately counted in `headcount`.

5. **One thing to change for 100x data.**
   Pre-aggregate daily attendance metrics into a daily summary collection using MongoDB change streams or a midnight rollup job. Analytics endpoints could then query pre-calculated daily metrics instead of aggregating raw punch-in and punch-out timestamps on the fly.
