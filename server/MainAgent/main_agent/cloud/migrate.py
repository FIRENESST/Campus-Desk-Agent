import hashlib
from pathlib import Path

import psycopg


MIGRATIONS = Path(__file__).resolve().parents[4] / "DataBase" / "PostgreSQL" / "migrations"


def migrate(dsn, directory=MIGRATIONS):
    """Explicit owner-only command; web processes never perform DDL at startup."""
    applied = []
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as connection:
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(731482906)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,checksum TEXT NOT NULL,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            for path in sorted(directory.glob("*.sql")):
                content = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(content.encode()).hexdigest()
                existing = connection.execute("SELECT checksum FROM schema_migrations WHERE version=%s", (path.name,)).fetchone()
                if existing:
                    if existing[0] != checksum:
                        raise ValueError("已执行的迁移文件校验失败；请新增迁移，不要修改历史迁移")
                    continue
                connection.execute(content, prepare=False)
                connection.execute("INSERT INTO schema_migrations(version,checksum) VALUES (%s,%s)", (path.name, checksum))
                applied.append(path.name)
    return applied
