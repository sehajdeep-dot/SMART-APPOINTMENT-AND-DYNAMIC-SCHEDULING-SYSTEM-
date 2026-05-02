#!/usr/bin/env python3
import json
import os
import sqlite3
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "dist"
DB_PATH = BASE_DIR / "smart_scheduler.db"
GRACE_MINUTES = 10
SLOT_STEP_MINUTES = 15


def now():
    return datetime.now().replace(second=0, microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def parse_dt(value):
    try:
        return datetime.strptime(value[:16], "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError("Use a valid date and time") from exc


def dict_rows(cursor):
    return [dict(row) for row in cursor.fetchall()]


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
DROP TRIGGER IF EXISTS trg_consultation_duration;
DROP TRIGGER IF EXISTS trg_consultation_complete_notification;
DROP TRIGGER IF EXISTS trg_slot_released_notice;
DROP VIEW IF EXISTS v_daily_queue_status;
DROP VIEW IF EXISTS v_doctor_performance;

CREATE TABLE IF NOT EXISTS patients (
    patient_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL CHECK (length(trim(name)) >= 2),
    phone TEXT NOT NULL UNIQUE CHECK (length(trim(phone)) >= 7),
    email TEXT UNIQUE CHECK (email IS NULL OR instr(email, '@') > 1),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS doctors (
    doctor_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    specialization TEXT NOT NULL,
    avg_consult_time INTEGER NOT NULL DEFAULT 3 CHECK (avg_consult_time BETWEEN 1 AND 90),
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS appointments (
    appointment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    doctor_id INTEGER NOT NULL REFERENCES doctors(doctor_id) ON DELETE CASCADE,
    scheduled_time TEXT NOT NULL,
    appointment_type TEXT NOT NULL DEFAULT 'SCHEDULED'
        CHECK (appointment_type IN ('SCHEDULED', 'WALK_IN', 'EMERGENCY')),
    status TEXT NOT NULL DEFAULT 'BOOKED'
        CHECK (status IN ('BOOKED', 'CHECKED_IN', 'IN_CONSULTATION', 'COMPLETED', 'CANCELLED', 'NO_SHOW')),
    priority INTEGER NOT NULL DEFAULT 1 CHECK (priority BETWEEN 0 AND 3),
    check_in_time TEXT,
    queue_position INTEGER,
    estimated_time TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_doctor_slot_active
ON appointments(doctor_id, scheduled_time)
WHERE appointment_type = 'SCHEDULED' AND status IN ('BOOKED', 'CHECKED_IN', 'IN_CONSULTATION');

CREATE INDEX IF NOT EXISTS idx_appointments_doctor_day ON appointments(doctor_id, scheduled_time);
CREATE INDEX IF NOT EXISTS idx_appointments_patient ON appointments(patient_id);
CREATE INDEX IF NOT EXISTS idx_appointments_checkin ON appointments(check_in_time);

CREATE TABLE IF NOT EXISTS consultations (
    consultation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL UNIQUE REFERENCES appointments(appointment_id) ON DELETE CASCADE,
    start_time TEXT NOT NULL,
    end_time TEXT,
    duration INTEGER CHECK (duration IS NULL OR duration >= 0)
);

CREATE TABLE IF NOT EXISTS doctor_availability (
    availability_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id INTEGER NOT NULL REFERENCES doctors(doctor_id) ON DELETE CASCADE,
    break_start_time TEXT NOT NULL,
    break_duration INTEGER NOT NULL CHECK (break_duration > 0),
    status TEXT NOT NULL DEFAULT 'UNAVAILABLE' CHECK (status IN ('AVAILABLE', 'UNAVAILABLE')),
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS delay_log (
    delay_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id INTEGER NOT NULL REFERENCES doctors(doctor_id) ON DELETE CASCADE,
    appointment_id INTEGER REFERENCES appointments(appointment_id) ON DELETE SET NULL,
    delay_minutes INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL REFERENCES appointments(appointment_id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    read_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_notifications_appointment ON notifications(appointment_id, created_at);
CREATE INDEX IF NOT EXISTS idx_delay_doctor ON delay_log(doctor_id, created_at);

CREATE VIEW IF NOT EXISTS v_daily_queue_status AS
SELECT
    a.appointment_id,
    p.name AS patient_name,
    d.name AS doctor_name,
    d.specialization,
    a.scheduled_time,
    a.appointment_type,
    a.status,
    a.queue_position,
    a.estimated_time
FROM appointments a
JOIN patients p ON p.patient_id = a.patient_id
JOIN doctors d ON d.doctor_id = a.doctor_id;

CREATE VIEW IF NOT EXISTS v_doctor_performance AS
SELECT
    d.doctor_id,
    d.name,
    d.specialization,
    COUNT(a.appointment_id) AS total_appointments,
    SUM(CASE WHEN a.status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed_appointments,
    ROUND(AVG(c.duration), 1) AS actual_avg_duration
FROM doctors d
LEFT JOIN appointments a ON a.doctor_id = d.doctor_id
LEFT JOIN consultations c ON c.appointment_id = a.appointment_id AND c.duration IS NOT NULL
GROUP BY d.doctor_id;

CREATE TRIGGER IF NOT EXISTS trg_consultation_duration
AFTER UPDATE OF end_time ON consultations
WHEN NEW.end_time IS NOT NULL
BEGIN
    UPDATE consultations
    SET duration = max(1, CAST((julianday(NEW.end_time) - julianday(NEW.start_time)) * 24 * 60 AS INTEGER))
    WHERE consultation_id = NEW.consultation_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_consultation_complete_notification
AFTER UPDATE OF end_time ON consultations
WHEN NEW.end_time IS NOT NULL AND OLD.end_time IS NULL
BEGIN
    INSERT INTO notifications(appointment_id, message)
    VALUES (NEW.appointment_id, 'Consultation completed. Thank you for visiting.');
END;

CREATE TRIGGER IF NOT EXISTS trg_slot_released_notice
AFTER UPDATE OF status ON appointments
WHEN NEW.status = 'CANCELLED'
BEGIN
    INSERT INTO notifications(appointment_id, message)
    VALUES (NEW.appointment_id, 'Appointment cancelled and slot released for reuse.');
END;
"""


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM doctors").fetchone()[0] == 0:
            seed(conn)
        recalc_all(conn)


def seed(conn):
    doctors = [
        ("Dr. Asha Mehta", "Cardiology", 3),
        ("Dr. Kabir Anand", "Orthopedics", 4),
        ("Dr. Naina Rao", "Dermatology", 3),
    ]
    patients = [
        ("Riya Sharma", "9876543210", "riya@example.com"),
        ("Arjun Malhotra", "9876543211", "arjun@example.com"),
        ("Meera Kapoor", "9876543212", "meera@example.com"),
        ("Ishaan Verma", "9876543213", "ishaan@example.com"),
        ("Tara Singh", "9876543214", "tara@example.com"),
    ]
    conn.executemany("INSERT INTO doctors(name, specialization, avg_consult_time) VALUES (?, ?, ?)", doctors)
    conn.executemany("INSERT INTO patients(name, phone, email) VALUES (?, ?, ?)", patients)
    base = now().replace(minute=0) + timedelta(minutes=30)
    slots = [
        (1, 1, base),
        (2, 1, base + timedelta(minutes=15)),
        (3, 1, base + timedelta(minutes=30)),
        (4, 2, base + timedelta(minutes=10)),
        (5, 3, base + timedelta(minutes=5)),
    ]
    for patient_id, doctor_id, at in slots:
        conn.execute(
            """INSERT INTO appointments(patient_id, doctor_id, scheduled_time, status, priority)
               VALUES (?, ?, ?, 'BOOKED', 1)""",
            (patient_id, doctor_id, iso(at)),
        )


def mark_no_shows(conn, doctor_id=None, day=None):
    current = now()
    args = []
    clause = "status = 'BOOKED' AND appointment_type = 'SCHEDULED' AND check_in_time IS NULL"
    if doctor_id:
        clause += " AND doctor_id = ?"
        args.append(doctor_id)
    if day:
        clause += " AND date(scheduled_time) = date(?)"
        args.append(day)
    rows = conn.execute(f"SELECT appointment_id, scheduled_time FROM appointments WHERE {clause}", args).fetchall()
    for row in rows:
        if current > parse_dt(row["scheduled_time"]) + timedelta(minutes=GRACE_MINUTES):
            conn.execute(
                "UPDATE appointments SET status = 'NO_SHOW', queue_position = NULL, estimated_time = NULL WHERE appointment_id = ?",
                (row["appointment_id"],),
            )
            conn.execute(
                "INSERT INTO notifications(appointment_id, message) VALUES (?, ?)",
                (row["appointment_id"], "Marked no-show after the grace period expired."),
            )


def shift_for_unavailability(conn, doctor_id, cursor_time, planned_minutes=0):
    rows = conn.execute(
        """SELECT break_start_time, break_duration FROM doctor_availability
           WHERE doctor_id = ? AND status = 'UNAVAILABLE'
           ORDER BY break_start_time""",
        (doctor_id,),
    ).fetchall()
    adjusted = cursor_time
    while True:
        moved = False
        planned_end = adjusted + timedelta(minutes=planned_minutes)
        for row in rows:
            start = parse_dt(row["break_start_time"])
            end = start + timedelta(minutes=row["break_duration"])
            starts_inside_break = start <= adjusted < end
            overlaps_upcoming_break = planned_minutes and adjusted < start < planned_end
            if starts_inside_break or overlaps_upcoming_break:
                adjusted = end
                moved = True
                break
        if not moved:
            return adjusted


def recalc_queue(conn, doctor_id, day):
    mark_no_shows(conn, doctor_id, day)
    avg = conn.execute("SELECT avg_consult_time FROM doctors WHERE doctor_id = ?", (doctor_id,)).fetchone()
    avg_minutes = avg["avg_consult_time"] if avg else 12
    rows = conn.execute(
        """SELECT * FROM appointments
           WHERE doctor_id = ?
             AND date(scheduled_time) = date(?)
             AND status IN ('BOOKED', 'CHECKED_IN', 'IN_CONSULTATION')
           ORDER BY
             CASE appointment_type WHEN 'EMERGENCY' THEN 0 WHEN 'SCHEDULED' THEN 1 ELSE 2 END,
             CASE WHEN appointment_type = 'SCHEDULED' THEN scheduled_time ELSE COALESCE(check_in_time, scheduled_time) END,
             scheduled_time,
             appointment_id""",
        (doctor_id, day),
    ).fetchall()
    cursor = max(now(), parse_dt(day + "T00:00") if len(day) == 10 else parse_dt(day))
    for index, row in enumerate(rows, start=1):
        scheduled = parse_dt(row["scheduled_time"])
        if row["appointment_type"] == "SCHEDULED":
            cursor = max(cursor, scheduled)
        if row["status"] == "IN_CONSULTATION":
            started = conn.execute(
                "SELECT start_time FROM consultations WHERE appointment_id = ? AND end_time IS NULL",
                (row["appointment_id"],),
            ).fetchone()
            cursor = parse_dt(started["start_time"]) if started else cursor
        if row["status"] != "IN_CONSULTATION":
            cursor = shift_for_unavailability(conn, doctor_id, cursor, avg_minutes)
        conn.execute(
            "UPDATE appointments SET queue_position = ?, estimated_time = ? WHERE appointment_id = ?",
            (index, iso(cursor), row["appointment_id"]),
        )
        cursor += timedelta(minutes=avg_minutes)


def recalc_all(conn):
    conn.execute(
        """UPDATE appointments
           SET queue_position = NULL, estimated_time = NULL
           WHERE status NOT IN ('BOOKED', 'CHECKED_IN', 'IN_CONSULTATION')"""
    )
    pairs = conn.execute(
        """SELECT DISTINCT doctor_id, date(scheduled_time) AS day
           FROM appointments
           WHERE status IN ('BOOKED', 'CHECKED_IN', 'IN_CONSULTATION')"""
    ).fetchall()
    for pair in pairs:
        recalc_queue(conn, pair["doctor_id"], pair["day"])


def recalc_for_appointment(conn, appointment_id):
    row = conn.execute("SELECT doctor_id, date(scheduled_time) AS day FROM appointments WHERE appointment_id = ?", (appointment_id,)).fetchone()
    if row:
        recalc_queue(conn, row["doctor_id"], row["day"])


def slot_conflict(conn, doctor_id, scheduled_time, exclude_appointment_id=None):
    args = [doctor_id, scheduled_time]
    appointment_filter = ""
    if exclude_appointment_id is not None:
        appointment_filter = "AND appointment_id <> ?"
        args.append(exclude_appointment_id)
    return conn.execute(
        f"""SELECT appointment_id FROM appointments
            WHERE doctor_id = ?
              AND scheduled_time = ?
              AND appointment_type = 'SCHEDULED'
              AND status IN ('BOOKED', 'CHECKED_IN', 'IN_CONSULTATION')
              {appointment_filter}""",
        args,
    ).fetchone()


def next_available_slot(conn, doctor_id, requested_at, exclude_appointment_id=None):
    candidate = requested_at.replace(second=0, microsecond=0)
    for _ in range(96):
        if not slot_conflict(conn, doctor_id, iso(candidate), exclude_appointment_id):
            return candidate
        candidate += timedelta(minutes=SLOT_STEP_MINUTES)
    raise ValueError("No available scheduled slot found in the next 24 hours")


def write_json(handler, payload, status=200):
    data = json.dumps(payload, default=str).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_json(handler):
    length = int(handler.headers.get("Content-Length", 0))
    if not length:
        return {}
    return json.loads(handler.rfile.read(length).decode())


class AppHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        parsed = urlparse(path).path
        if parsed == "/":
            return str(DIST_DIR / "index.html")
        target = DIST_DIR / parsed.lstrip("/")
        if not target.exists() and not parsed.startswith("/assets/"):
            return str(DIST_DIR / "index.html")
        return str(target)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            try:
                self.handle_api_get(parsed.path, parse_qs(parsed.query))
            except Exception as exc:
                write_json(self, {"error": str(exc)}, 500)
            return
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            write_json(self, {"error": "Not found"}, 404)
            return
        try:
            self.handle_api_post(parsed.path, read_json(self))
        except sqlite3.IntegrityError as exc:
            write_json(self, {"error": f"Database constraint failed: {exc}"}, 409)
        except ValueError as exc:
            write_json(self, {"error": str(exc)}, 400)
        except Exception as exc:
            write_json(self, {"error": str(exc)}, 500)

    def handle_api_get(self, path, query):
        with connect() as conn:
            recalc_all(conn)
            if path == "/api/bootstrap":
                write_json(self, {
                    "doctors": dict_rows(conn.execute("SELECT * FROM doctors ORDER BY name")),
                    "patients": dict_rows(conn.execute("SELECT * FROM patients ORDER BY created_at DESC")),
                    "queue": queue_payload(conn),
                    "reports": reports_payload(conn),
                })
            elif path == "/api/queue":
                write_json(self, queue_payload(conn, query.get("doctor_id", [None])[0]))
            elif path == "/api/reports":
                write_json(self, reports_payload(conn))
            elif path == "/api/appointments":
                write_json(self, appointments_payload(conn))
            else:
                write_json(self, {"error": "Unknown endpoint"}, 404)

    def handle_api_post(self, path, data):
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if path == "/api/patients":
                cur = conn.execute(
                    "INSERT INTO patients(name, phone, email) VALUES (?, ?, ?)",
                    (data["name"].strip(), data["phone"].strip(), data.get("email") or None),
                )
                conn.commit()
                write_json(self, {"patient_id": cur.lastrowid})
            elif path == "/api/appointments":
                appointment = book_appointment(conn, data)
                conn.commit()
                write_json(self, {**appointment, "queue": queue_payload(conn)})
            elif path == "/api/checkin":
                appointment_id = int(data["appointment_id"])
                process_checkin(conn, appointment_id)
                conn.commit()
                write_json(self, {"ok": True, "queue": queue_payload(conn)})
            elif path == "/api/consultations/start":
                appointment_id = int(data["appointment_id"])
                start_consultation(conn, appointment_id)
                conn.commit()
                write_json(self, {"ok": True, "queue": queue_payload(conn)})
            elif path == "/api/consultations/finish":
                appointment_id = int(data["appointment_id"])
                finish_consultation(conn, appointment_id)
                conn.commit()
                write_json(self, {"ok": True, "queue": queue_payload(conn), "reports": reports_payload(conn)})
            elif path == "/api/appointments/cancel":
                appointment_id = int(data["appointment_id"])
                row = conn.execute("SELECT status FROM appointments WHERE appointment_id = ?", (appointment_id,)).fetchone()
                if not row:
                    raise ValueError("Appointment not found")
                if row["status"] in ("COMPLETED", "IN_CONSULTATION"):
                    raise ValueError("Completed or active consultations cannot be cancelled")
                conn.execute("UPDATE appointments SET status = 'CANCELLED', queue_position = NULL, estimated_time = NULL WHERE appointment_id = ?", (appointment_id,))
                recalc_for_appointment(conn, appointment_id)
                conn.commit()
                write_json(self, {"ok": True, "queue": queue_payload(conn)})
            elif path == "/api/appointments/reschedule":
                appointment_id = int(data["appointment_id"])
                requested_time = parse_dt(data["scheduled_time"])
                row = conn.execute(
                    "SELECT doctor_id, status, scheduled_time FROM appointments WHERE appointment_id = ?",
                    (appointment_id,),
                ).fetchone()
                if not row:
                    raise ValueError("Appointment not found")
                if row["status"] in ("COMPLETED", "IN_CONSULTATION"):
                    raise ValueError("Completed or active consultations cannot be rescheduled")
                new_time = iso(next_available_slot(conn, row["doctor_id"], requested_time, appointment_id))
                conn.execute(
                    """UPDATE appointments
                       SET scheduled_time = ?, status = 'BOOKED', appointment_type = 'SCHEDULED',
                           priority = 1, check_in_time = NULL
                       WHERE appointment_id = ? AND status NOT IN ('COMPLETED', 'IN_CONSULTATION')""",
                    (new_time, appointment_id),
                )
                recalc_queue(conn, row["doctor_id"], row["scheduled_time"][:10])
                if new_time[:10] != row["scheduled_time"][:10]:
                    recalc_queue(conn, row["doctor_id"], new_time[:10])
                conn.commit()
                write_json(self, {
                    "ok": True,
                    "appointment_id": appointment_id,
                    "requested_time": iso(requested_time),
                    "scheduled_time": new_time,
                    "adjusted": new_time != iso(requested_time),
                    "queue": queue_payload(conn),
                })
            elif path == "/api/availability":
                doctor_id = int(data["doctor_id"])
                minutes = int(data.get("break_duration", 15))
                start = data.get("break_start_time") or iso(now())
                cur = conn.execute(
                    """INSERT INTO doctor_availability(doctor_id, break_start_time, break_duration, status, reason)
                       VALUES (?, ?, ?, 'UNAVAILABLE', ?)""",
                    (doctor_id, start, minutes, data.get("reason") or "Break / emergency"),
                )
                impacted = conn.execute(
                    """SELECT appointment_id FROM appointments
                       WHERE doctor_id = ? AND status IN ('BOOKED', 'CHECKED_IN') AND datetime(scheduled_time) >= datetime(?)""",
                    (doctor_id, start),
                ).fetchall()
                for row in impacted:
                    conn.execute(
                        "INSERT INTO notifications(appointment_id, message) VALUES (?, ?)",
                        (row["appointment_id"], f"Doctor unavailable for {minutes} minutes. Estimated time was recalculated."),
                    )
                conn.execute(
                    "INSERT INTO delay_log(doctor_id, delay_minutes, message) VALUES (?, ?, ?)",
                    (doctor_id, minutes, data.get("reason") or "Doctor unavailable; queue recalculated."),
                )
                recalc_queue(conn, doctor_id, start[:10])
                conn.commit()
                write_json(self, {"availability_id": cur.lastrowid, "queue": queue_payload(conn)})
            else:
                conn.rollback()
                write_json(self, {"error": "Unknown endpoint"}, 404)


def book_appointment(conn, data):
    patient_id = int(data["patient_id"])
    doctor_id = int(data["doctor_id"])
    appointment_type = data.get("appointment_type", "SCHEDULED").upper()
    if appointment_type not in ("SCHEDULED", "WALK_IN", "EMERGENCY"):
        raise ValueError("Appointment type must be SCHEDULED, WALK_IN, or EMERGENCY")
    if not conn.execute("SELECT 1 FROM patients WHERE patient_id = ?", (patient_id,)).fetchone():
        raise ValueError("Patient not found")
    if not conn.execute("SELECT 1 FROM doctors WHERE doctor_id = ? AND is_active = 1", (doctor_id,)).fetchone():
        raise ValueError("Doctor not found or inactive")
    requested_time = parse_dt(data.get("scheduled_time") or iso(now()))
    scheduled_time = iso(requested_time)
    if appointment_type == "SCHEDULED":
        scheduled_time = iso(next_available_slot(conn, doctor_id, requested_time))
    priority = 0 if appointment_type == "EMERGENCY" else (2 if appointment_type == "WALK_IN" else 1)
    status = "CHECKED_IN" if appointment_type in ("WALK_IN", "EMERGENCY") else "BOOKED"
    check_in_time = iso(now()) if appointment_type in ("WALK_IN", "EMERGENCY") else None
    cur = conn.execute(
        """INSERT INTO appointments(patient_id, doctor_id, scheduled_time, appointment_type, status, priority, check_in_time, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (patient_id, doctor_id, scheduled_time, appointment_type, status, priority, check_in_time, data.get("notes")),
    )
    recalc_for_appointment(conn, cur.lastrowid)
    return {
        "appointment_id": cur.lastrowid,
        "requested_time": iso(requested_time),
        "scheduled_time": scheduled_time,
        "adjusted": scheduled_time != iso(requested_time),
    }


def process_checkin(conn, appointment_id):
    row = conn.execute("SELECT * FROM appointments WHERE appointment_id = ?", (appointment_id,)).fetchone()
    if not row:
        raise ValueError("Appointment not found")
    if row["status"] not in ("BOOKED", "NO_SHOW"):
        raise ValueError("Only booked or no-show appointments can be checked in")
    arrival = now()
    scheduled = parse_dt(row["scheduled_time"])
    if arrival <= scheduled + timedelta(minutes=GRACE_MINUTES) and row["status"] != "NO_SHOW":
        conn.execute("UPDATE appointments SET status = 'CHECKED_IN', check_in_time = ? WHERE appointment_id = ?", (iso(arrival), appointment_id))
    else:
        conn.execute(
            """UPDATE appointments
               SET status = 'CHECKED_IN', appointment_type = 'WALK_IN', priority = 2, check_in_time = ?
               WHERE appointment_id = ?""",
            (iso(arrival), appointment_id),
        )
        conn.execute(
            "INSERT INTO notifications(appointment_id, message) VALUES (?, ?)",
            (appointment_id, "Late arrival converted to walk-in according to grace-period rules."),
        )
    recalc_for_appointment(conn, appointment_id)


def start_consultation(conn, appointment_id):
    row = conn.execute("SELECT status FROM appointments WHERE appointment_id = ?", (appointment_id,)).fetchone()
    if not row or row["status"] not in ("CHECKED_IN", "BOOKED"):
        raise ValueError("Appointment must be booked or checked in before consultation starts")
    active = conn.execute(
        """SELECT a.appointment_id FROM appointments a
           JOIN consultations c ON c.appointment_id = a.appointment_id
           WHERE a.doctor_id = (SELECT doctor_id FROM appointments WHERE appointment_id = ?)
             AND a.status = 'IN_CONSULTATION' AND c.end_time IS NULL""",
        (appointment_id,),
    ).fetchone()
    if active:
        raise ValueError(f"Doctor already has appointment #{active['appointment_id']} in consultation")
    conn.execute("UPDATE appointments SET status = 'IN_CONSULTATION', check_in_time = COALESCE(check_in_time, ?) WHERE appointment_id = ?", (iso(now()), appointment_id))
    conn.execute("INSERT INTO consultations(appointment_id, start_time) VALUES (?, ?)", (appointment_id, iso(now())))
    recalc_for_appointment(conn, appointment_id)


def finish_consultation(conn, appointment_id):
    row = conn.execute(
        "SELECT c.consultation_id, c.start_time, a.doctor_id FROM consultations c JOIN appointments a ON a.appointment_id = c.appointment_id WHERE c.appointment_id = ? AND c.end_time IS NULL",
        (appointment_id,),
    ).fetchone()
    if not row:
        raise ValueError("No active consultation found")
    end = now()
    duration = max(1, int((end - parse_dt(row["start_time"])).total_seconds() // 60))
    conn.execute("UPDATE consultations SET end_time = ?, duration = ? WHERE consultation_id = ?", (iso(end), duration, row["consultation_id"]))
    conn.execute("UPDATE appointments SET status = 'COMPLETED', queue_position = NULL, estimated_time = NULL WHERE appointment_id = ?", (appointment_id,))
    avg = conn.execute(
        """SELECT ROUND(AVG(duration)) AS avg_duration
           FROM (SELECT duration FROM consultations c
                 JOIN appointments a ON a.appointment_id = c.appointment_id
                 WHERE a.doctor_id = ? AND duration IS NOT NULL
                 ORDER BY c.consultation_id DESC LIMIT 10)""",
        (row["doctor_id"],),
    ).fetchone()["avg_duration"]
    if avg:
        conn.execute("UPDATE doctors SET avg_consult_time = ? WHERE doctor_id = ?", (int(avg), row["doctor_id"]))
    recalc_queue(conn, row["doctor_id"], iso(end)[:10])


def queue_payload(conn, doctor_id=None):
    args = []
    where = "date(a.scheduled_time) = date('now', 'localtime') OR a.status IN ('BOOKED', 'CHECKED_IN', 'IN_CONSULTATION')"
    if doctor_id:
        where = f"({where}) AND a.doctor_id = ?"
        args.append(doctor_id)
    rows = conn.execute(
        f"""SELECT a.*, p.name AS patient_name, p.phone, d.name AS doctor_name, d.specialization
            FROM appointments a
            JOIN patients p ON p.patient_id = a.patient_id
            JOIN doctors d ON d.doctor_id = a.doctor_id
            WHERE {where}
            ORDER BY a.status IN ('COMPLETED','CANCELLED','NO_SHOW'), d.name, a.queue_position IS NULL, a.queue_position, a.scheduled_time""",
        args,
    ).fetchall()
    notifications = dict_rows(conn.execute(
        """SELECT n.*, p.name AS patient_name
           FROM notifications n
           JOIN appointments a ON a.appointment_id = n.appointment_id
           JOIN patients p ON p.patient_id = a.patient_id
           ORDER BY n.created_at DESC LIMIT 8"""
    ))
    return {"items": [dict(row) for row in rows], "notifications": notifications}


def appointments_payload(conn):
    return dict_rows(conn.execute(
        """SELECT a.appointment_id, p.name AS patient_name, d.name AS doctor_name, a.scheduled_time, a.status
           FROM appointments a
           JOIN patients p ON p.patient_id = a.patient_id
           JOIN doctors d ON d.doctor_id = a.doctor_id
           ORDER BY a.created_at DESC LIMIT 50"""
    ))


def reports_payload(conn):
    stats = conn.execute(
        """SELECT
            COUNT(*) AS total,
            SUM(status = 'BOOKED') AS booked,
            SUM(status = 'CHECKED_IN') AS checked_in,
            SUM(status = 'IN_CONSULTATION') AS in_consultation,
            SUM(status = 'COMPLETED') AS completed,
            SUM(status = 'CANCELLED') AS cancelled,
            SUM(status = 'NO_SHOW') AS no_show
           FROM appointments"""
    ).fetchone()
    high_volume = dict_rows(conn.execute(
        """SELECT d.name, COUNT(a.appointment_id) AS appointment_count
           FROM doctors d JOIN appointments a ON a.doctor_id = d.doctor_id
           GROUP BY d.doctor_id HAVING COUNT(a.appointment_id) >= 2
           ORDER BY appointment_count DESC"""
    ))
    performance = dict_rows(conn.execute("SELECT * FROM v_doctor_performance ORDER BY name"))
    above_avg_wait = dict_rows(conn.execute(
        """SELECT p.name AS patient_name, d.name AS doctor_name, a.estimated_time, a.scheduled_time,
                  CAST((julianday(a.estimated_time) - julianday(a.scheduled_time)) * 24 * 60 AS INTEGER) AS delay_minutes
           FROM appointments a
           JOIN patients p ON p.patient_id = a.patient_id
           JOIN doctors d ON d.doctor_id = a.doctor_id
           WHERE a.estimated_time IS NOT NULL
             AND delay_minutes > (
                SELECT COALESCE(AVG(CAST((julianday(estimated_time) - julianday(scheduled_time)) * 24 * 60 AS INTEGER)), 0)
                FROM appointments WHERE estimated_time IS NOT NULL
             )
           ORDER BY delay_minutes DESC LIMIT 6"""
    ))
    return {
        "stats": dict(stats),
        "highVolumeDoctors": high_volume,
        "performance": performance,
        "aboveAverageWait": above_avg_wait,
    }


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), AppHandler)
    print(f"Smart Scheduler running at http://127.0.0.1:{port}")
    server.serve_forever()
