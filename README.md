# Smart Appointment and Dynamic Scheduling System

A complete DBMS project for a hospital or clinic reception desk. It manages patient registration, scheduled appointments, walk-ins, emergency priority, live queue positions, doctor delays, cancellations, rescheduling, consultation timing, no-shows, notifications, and reports.

## Tech Stack

- React + Vite frontend
- Python `http.server` backend API
- SQLite local demo database
- Oracle PL/SQL submission script

## Project Structure

```text
server.py                         Python API and static file server
src/                              React dashboard source
sql/oracle_plsql_project.sql      Oracle DDL, package, triggers, seed data, queries
dist/                             Production frontend after npm run build
smart_scheduler.db                Auto-created SQLite demo database
```

## Run Backend Only

The backend creates the SQLite schema and seed data automatically, then serves the built frontend from `dist/` when it exists.

```bash
python3 server.py
```

Open:

```text
http://127.0.0.1:8000
```

If port `8000` is already busy:

```bash
PORT=8001 python3 server.py
```

## Run React/Vite Development Frontend

Use two terminals.

Terminal 1:

```bash
python3 server.py
```

Terminal 2:

```bash
npm install
npm run dev
```

Open:

```text
http://127.0.0.1:5173
```

Vite proxies `/api` calls to the Python backend on port `8000`.

## Build Production Frontend

```bash
npm install
npm run build
python3 server.py
```

After the build, Python serves `dist/index.html` and the compiled assets directly from:

```text
http://127.0.0.1:8000
```

## Main API Routes

- `GET /api/bootstrap`
- `GET /api/queue`
- `GET /api/reports`
- `GET /api/appointments`
- `POST /api/patients`
- `POST /api/appointments`
- `POST /api/checkin`
- `POST /api/consultations/start`
- `POST /api/consultations/finish`
- `POST /api/appointments/cancel`
- `POST /api/appointments/reschedule`
- `POST /api/availability`

## How To Use The App

1. Register a patient from the Patient tab.
2. Book a scheduled, walk-in, or emergency appointment from the Book tab.
3. Watch the live queue update with position, ETA, doctor, specialization, patient phone, and status.
4. Use row actions to check in, start, finish, move, cancel, or reschedule appointments.
5. Add doctor unavailability from the Delay tab to log delays, notify patients, and recalculate ETAs.
6. Review doctor performance, notifications, and above-average wait analytics at the bottom of the dashboard.

## Database Features

SQLite includes normalized tables for patients, doctors, appointments, consultations, doctor availability, delay logs, and notifications. It uses primary keys, foreign keys, check constraints, unique constraints, indexes, a partial unique index that prevents active scheduled double-booking, views, and triggers for consultation duration and cancellation/completion notifications.

## Oracle PL/SQL Submission

Use this file for the DBMS submission:

```text
sql/oracle_plsql_project.sql
```

It contains full Oracle DDL, constraints, indexes, views, package `smart_scheduler_pkg`, procedures, functions, explicit cursors, triggers, seed data, and example view queries.

## Package For Submission

```bash
zip -r smart-appointment-dynamic-scheduling-system.zip . \
  -x "node_modules/*" "__pycache__/*" "*/__pycache__/*" "*.DS_Store" "smart-appointment-dynamic-scheduling-system.zip"
```
