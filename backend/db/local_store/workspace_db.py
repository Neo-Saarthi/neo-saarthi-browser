import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Set


DEFAULT_DB_PATH = Path(os.environ["COMPANION_DATA_DIR"]) / "browser.sqlite3"
_READY: Set[Path] = set()
_LOCK = threading.RLock()
_SCHEMA = json.loads(Path(__file__).with_name("schema.json").read_text(encoding="utf-8"))


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def initialize_database(db_path: Path = DEFAULT_DB_PATH) -> None:
    path = Path(db_path).resolve()
    with _LOCK:
        if path in _READY and path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        database = connect(path)
        try:
            database.execute("PRAGMA journal_mode = WAL")
            for statement in _SCHEMA:
                database.execute(statement)
            columns = {str(row["name"]) for row in database.execute("PRAGMA table_info(browser_use_clients)")}
            if "paused" not in columns:
                database.execute("ALTER TABLE browser_use_clients ADD COLUMN paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1))")
            database.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_browser_recording ON browser_use_workflows(distributor_id) WHERE status = 'recording'")
            database.commit()
            _READY.add(path)
        finally:
            database.close()


def local_owner_distributor_id(database: sqlite3.Connection) -> str:
    database.execute("SELECT 1")
    return "local-browser-owner"
