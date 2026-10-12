# Employee Attendance & Analytics API

## Overview

A FastAPI service backed by MongoDB that manages employee records, daily attendance, and analytics (monthly summaries, department dashboards, late-comer leaderboard, daily trend with gap filling).

Read in this order before contributing:

1. `PROBLEM_STATEMENT.docx` – what to build and how it is evaluated.
2. `openapi.yaml` – the HTTP contract (paths, status codes, request/response schemas, business rules R1–R10).
3. `DATA_MODEL.md` – MongoDB collections, field types, sample documents.
4. `app/main.py` – all application code lives here.
5. `REVIEW.md`, `DECISIONS.md` – bug log and design decisions.

---

## Requirements

- **Python** 3.10+
- **MongoDB** 6.0 or newer (the grader uses 7.0). A local Docker container or MongoDB Atlas free tier (M0) both work.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | Full MongoDB connection string. |
| `MONGO_DB` | `attendance_db` | Name of the database to use. |

Do **not** commit a `.env` file containing real credentials. Add `.env` to `.gitignore`.

---

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start MongoDB (local Docker example)
docker run -d -p 27017:27017 --name mongo7 mongo:7

# 4. Configure environment
# Copy the example and set your values (never commit real credentials)
cp .env.example .env          # or create .env manually
# Minimum .env contents:
#   MONGO_URI=mongodb://localhost:27017
#   MONGO_DB=attendance_db

# 5. Seed the nine sample documents
python sample_seed.py

# 6. Start the development server
uvicorn app.main:app --port 8000 --reload
```

Indexes are created idempotently at startup; you do not need to create them manually.

---

## Running tests

```bash
# Run the complete test suite (all four test files)
pytest tests/ -v

# Run a single step
pytest tests/test_step1.py -v
pytest tests/test_step2a.py -v
pytest tests/test_step3.py -v
pytest tests/test_step4.py -v
```

The tests connect to the same MongoDB instance used by the app (reads `MONGO_URI` / `MONGO_DB` from the environment or `.env`). Each test file cleans up its own test documents before and after every test.

---

## API documentation

Once the server is running, open:

- **Swagger UI**: <http://localhost:8000/docs>
- **ReDoc**: <http://localhost:8000/redoc>
- **OpenAPI JSON**: <http://localhost:8000/openapi.json>

The canonical contract is `openapi.yaml` in this repository.

---

## Submission

A public Git repository containing:
- `app/main.py`
- `requirements.txt`
- `REVIEW.md`
- `DECISIONS.md`
- `README.md`

**Do not include**: `.env`, database dumps, secrets, or a `Dockerfile`.
