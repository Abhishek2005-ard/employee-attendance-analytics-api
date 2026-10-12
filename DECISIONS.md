# DECISIONS.md

## 1. Indexes

Six indexes are created idempotently at startup via `init_db()` (called in FastAPI `lifespan` and on import):

**employees collection**
- `emp_code` unique – enforces uniqueness, supports O(1) employee lookups by `emp_code`.
- `department` – supports department-filter queries in `list_employees` and `department_summary`.
- `joined_on` – supports `department_summary` and `department_trend` headcount calculations that filter `joined_on <= month_end` (or `<= day`). Without this index, every headcount lookup in the trend pipeline scans the whole employees collection.

**attendance_logs collection**
- `(emp_code, date)` unique – enforces one record per employee per day; concurrent punch-ins for the same date race to `insert_one` and the loser gets `DuplicateKeyError` (409), no application-level locking needed.
- `(date DESC, emp_code ASC)` – covers the default sort for `attendance_list` (no extra SORT stage), and provides an efficient date-range entry point for `late_leaderboard` and `department_trend` initial `$match` stages.
- `status` – supports queries filtered by status only (e.g. `attendance_list?status=ABSENT`).

## 2. Punch-in race condition

Both concurrent requests compute the attendance date and call `insert_one`. The unique compound index `(emp_code, date)` guarantees that only one succeeds (201 Created); the other raises `DuplicateKeyError`, which is caught and returned as 409 Conflict.

## 3. Standard competition ranking (ties)

`$setWindowFields` with `$rank` produces standard competition ranking (1, 2, 2, 4). The `$match { rank: { $lte: limit } }` stage runs *after* ranking, so all employees tied at a rank ≤ limit are included even if the result count exceeds `limit`.

## 4. Headcount (R9)

The `department_summary` pipeline starts on `employees` filtered by `joined_on <= month_end`, then uses `$lookup` with `preserveNullAndEmptyArrays: true` and groups by `(department, emp_code)`. Employees with zero attendance logs still appear in the first group (headcount = 1) because `preserveNullAndEmptyArrays` keeps unmatched rows. The second `$group` sums headcounts per department.

## 5. GET /admin/explain/{endpoint}

The endpoint name is validated against a hardcoded `frozenset` allowlist of five names (exactly the `enum` in `openapi.yaml`). No user-supplied pipeline or query is ever executed raw. Each branch re-builds the *same* match filter / pipeline the live endpoint uses (using the same parameter parsing) and calls `.explain("executionStats")` (for `find`) or `aggregate(..., explain=True)` (for aggregation pipelines), then returns `{ endpoint, collection, explain }` after converting non-JSON-serialisable BSON types via `_bson_clean`.

## 6. One thing you would change for 100x data

At 100x data (~10 M documents) we would materialise per-month / per-department summary documents using `$merge` triggered at punch-out time (or via a nightly background job), so that analytics endpoints read a tiny pre-aggregated collection instead of scanning millions of attendance logs on every request.
