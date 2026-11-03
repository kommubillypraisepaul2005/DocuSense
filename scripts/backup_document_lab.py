"""Create a consistent local backup of the job database and saved JSON results."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
database_setting = Path(os.getenv("DATABASE_PATH", "data/docintel.sqlite3"))
DATABASE = database_setting if database_setting.is_absolute() else ROOT / database_setting
RESULTS = ROOT / "data" / "results"
BACKUPS = ROOT / "data" / "backups"


def main() -> None:
    if not DATABASE.is_file():
        raise SystemExit(f"Database does not exist: {DATABASE}")
    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = BACKUPS / f"document-lab-{stamp}.zip"
    with tempfile.TemporaryDirectory(prefix="document-lab-backup-") as temporary:
        snapshot = Path(temporary) / "docintel.sqlite3"
        with closing(sqlite3.connect(f"{DATABASE.as_uri()}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target)
                target.commit()
                integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SystemExit(f"Database snapshot failed integrity check: {integrity}")
        results = sorted(RESULTS.glob("*.json")) if RESULTS.exists() else []
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, "data/docintel.sqlite3")
            for result in results:
                archive.write(result, f"data/results/{result.name}")
            manifest = (
                "Open Document Intelligence backup\n"
                f"Created UTC: {datetime.now(timezone.utc).isoformat()}\n"
                f"Database integrity: {integrity}\n"
                f"Result JSON files: {len(results)}\n"
                "Original uploaded files are not stored by the application and are not included.\n"
                "If DATA_ENCRYPTION_KEY is configured, retain that same key separately; encrypted records cannot be opened without it.\n"
            )
            archive.writestr("BACKUP-README.txt", manifest)
    print(f"Backup created: {destination}")
    print(f"Included database and {len(results)} result JSON file(s).")


if __name__ == "__main__":
    main()
