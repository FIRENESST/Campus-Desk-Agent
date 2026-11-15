"""Storage paths and process coordination shared by RAG readers and writers."""

from contextlib import nullcontext
from pathlib import Path

from filelock import FileLock


DEFAULT_URI = Path(__file__).resolve().parent / "data" / "campus.db"
DEFAULT_COLLECTION = "campus_chunks"


def database_access_lock(uri: str, *, timeout: float = 30):
    if "://" in uri:
        return nullcontext()
    path = Path(uri).resolve()
    return FileLock(f"{path}.access.lock", timeout=timeout)
