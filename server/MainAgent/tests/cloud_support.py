"""Disposable PostgreSQL cluster; never reads the project's database credentials."""
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


class TemporaryPostgres:
    def __enter__(self):
        self.project = Path(__file__).resolve().parents[3]
        self.bin = Path(os.getenv("CLOUD_TEST_POSTGRES_BIN") or self.project / "DataBase/PostgreSQL/pgsql/bin")
        self.directory = Path(tempfile.mkdtemp(prefix=".cloud-test-", dir=self.project / "server/MainAgent")).resolve()
        self.password = secrets.token_urlsafe(32)
        password_file = self.directory / "password"
        password_file.write_text(self.password, encoding="ascii")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.started = False
        try:
            self._command("initdb", "-D", str(self.directory / "data"), "-U", "postgres",
                          "--pwfile", str(password_file), "--auth=scram-sha-256", "--no-locale", "--encoding=UTF8")
            password_file.unlink()
            self.started = True
            self._command("pg_ctl", "-D", str(self.directory / "data"), "-l", str(self.directory / "postgres.log"),
                          "-o", f"-h 127.0.0.1 -p {self.port}", "-w", "start")
            self.started = True
            self.admin = self.dsn("postgres", "postgres")
            with psycopg.connect(self.admin, autocommit=True) as connection:
                connection.execute(sql.SQL("CREATE ROLE agent_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}").format(sql.Literal(self.password)))
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _command(self, name, *args):
        result = subprocess.run([str(self.bin / (name + ".exe")), *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=45)
        if result.returncode:
            raise RuntimeError(f"Disposable PostgreSQL {name} failed (exit {result.returncode})")

    def dsn(self, database, user):
        return make_conninfo(host="127.0.0.1", port=self.port, dbname=database, user=user, password=self.password)

    def create_database(self):
        name = "test_" + uuid4().hex
        with psycopg.connect(self.admin, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name)))
        return name, self.dsn(name, "postgres"), self.dsn(name, "agent_app")

    def drop_database(self, name):
        if not name.startswith("test_") or len(name) != 37:
            raise ValueError("Refusing to remove a non-test database")
        with psycopg.connect(self.admin, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))

    def __exit__(self, *args):
        if self.started:
            self._command("pg_ctl", "-D", str(self.directory / "data"), "-m", "fast", "-w", "stop")
            self.started = False
        # Verify the absolute target before recursive cleanup on Windows.
        expected_parent = (self.project / "server/MainAgent").resolve()
        if self.directory.parent != expected_parent or not self.directory.name.startswith(".cloud-test-"):
            raise ValueError("Unsafe temporary PostgreSQL cleanup path")
        shutil.rmtree(self.directory)
