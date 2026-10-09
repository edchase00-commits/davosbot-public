"""Install occurrence schema around synthetic pre-existing scheduler jobs."""

from contextlib import closing
import sqlite3
from unittest.mock import patch

from davosbot import db


def install_legacy_timing_schema(db_path):
    def migrate(sql, _description):
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(sql)
            conn.commit()
    # These fixtures model jobs that were saved before the simulated due time.
    with patch.object(db, "_CRON_SQL_NOW", "'2000-01-01 00:00:00'"):
        db.init_legacy_cron_schema(db_path, migrate)
