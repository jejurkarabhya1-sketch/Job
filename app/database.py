import sqlite3
import os
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(os.getenv("JOBPILOT_DB_PATH", str(Path(__file__).resolve().parent.parent / "jobpilot.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with connect() as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT UNIQUE NOT NULL,
            source TEXT, title TEXT NOT NULL, company TEXT, location TEXT,
            date_posted TEXT, job_url TEXT NOT NULL, description TEXT,
            min_amount REAL, max_amount REAL, currency TEXT,
            is_remote INTEGER DEFAULT 0, experience_range TEXT,
            search_location TEXT DEFAULT '',
            skills TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        job_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "experience_range" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN experience_range TEXT")
        if "search_location" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN search_location TEXT DEFAULT ''")
        db.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_term TEXT NOT NULL, location TEXT, sources TEXT,
            result_count INTEGER DEFAULT 0, status TEXT,
            error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY CHECK (id=1),
            enabled INTEGER DEFAULT 0, search_term TEXT DEFAULT 'DevOps Engineer',
            location TEXT DEFAULT 'Worldwide', sources TEXT DEFAULT 'linkedin,indeed,jobicy,remoteok',
            interval_minutes INTEGER DEFAULT 360, last_run TEXT
        )""")
        db.execute("INSERT OR IGNORE INTO schedules(id) VALUES(1)")
