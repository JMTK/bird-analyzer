"""Storage backends for audio/metadata documents.

Provides a common interface so analyze.py and server.py can persist and
query recordings either through Elasticsearch or a local SQLite database,
selected via the `storage_backend` config option.
"""

import datetime
import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

try:
  import elasticsearch
except ImportError:
  elasticsearch = None


def _json_default(value):
  if isinstance(value, datetime.datetime):
    return value.isoformat()
  raise TypeError(f"Object of type {type(value)} is not JSON serializable")


def _dumps(document: dict) -> str:
  return json.dumps(document, default=_json_default)


def _stringify_timestamp(value) -> str:
  if isinstance(value, datetime.datetime):
    return value.isoformat()
  return str(value) if value is not None else ""


class Storage:
  """Common interface implemented by each storage backend."""

  def ping(self) -> bool:
    raise NotImplementedError

  def setup(self) -> None:
    raise NotImplementedError

  def index_audio(self, document: dict, doc_id: str) -> None:
    raise NotImplementedError

  def update_audio(self, doc_id: str, fields: dict) -> None:
    raise NotImplementedError

  def index_metadata(self, document: dict) -> None:
    raise NotImplementedError

  def fetch_audio(self, size: int = 250) -> list[dict]:
    raise NotImplementedError

  def fetch_metadata(self, size: int = 250) -> list[dict]:
    raise NotImplementedError


class ElasticsearchStorage(Storage):
  def __init__(self, host: str, user: str, password: str, cert_loc: str, audio_index: str, metadata_index: str):
    if elasticsearch is None:
      raise RuntimeError("elasticsearch package is not installed")
    self.client = elasticsearch.Elasticsearch(
      host,
      ca_certs=cert_loc,
      http_auth=(user, password),
      max_retries=0,
      retry_on_timeout=False,
      request_timeout=1,
    )
    self.audio_index = audio_index
    self.metadata_index = metadata_index

  def ping(self) -> bool:
    try:
      return bool(self.client.ping())
    except Exception:
      return False

  def setup(self) -> None:
    for index_name in (self.audio_index, self.metadata_index):
      try:
        self.client.indices.create(index=index_name)
      except Exception:
        print(f"{index_name} already exists, skipping creation")

  def index_audio(self, document: dict, doc_id: str) -> None:
    try:
      self.client.index(index=self.audio_index, id=doc_id, document=document)
    except Exception as exc:
      print(f"Failed indexing to {self.audio_index}: {exc}")

  def update_audio(self, doc_id: str, fields: dict) -> None:
    try:
      self.client.update(index=self.audio_index, id=doc_id, doc=fields, doc_as_upsert=True)
    except Exception as exc:
      print(f"Failed update to {self.audio_index}/{doc_id}: {exc}")

  def index_metadata(self, document: dict) -> None:
    try:
      self.client.index(index=self.metadata_index, document=document)
    except Exception as exc:
      print(f"Failed indexing to {self.metadata_index}: {exc}")

  def fetch_audio(self, size: int = 250) -> list[dict]:
    return self._fetch(self.audio_index, size)

  def fetch_metadata(self, size: int = 250) -> list[dict]:
    return self._fetch(self.metadata_index, size)

  def _fetch(self, index_name: str, size: int) -> list[dict]:
    try:
      response = self.client.search(
        index=index_name,
        size=size,
        sort=[{"timestamp": {"order": "desc", "unmapped_type": "date"}}],
        query={"match_all": {}},
      )
      hits = response.get("hits", {}).get("hits", [])
      docs = []
      for hit in hits:
        item = hit.get("_source", {})
        item["_id"] = hit.get("_id")
        docs.append(item)
      return docs
    except Exception:
      return []


class SqliteStorage(Storage):
  """Stores documents as JSON blobs, mirroring Elasticsearch's flexible schema."""

  def __init__(self, db_path: str):
    self.db_path = Path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    # Single shared connection guarded by a lock; sqlite3 connections aren't safe
    # for concurrent use from multiple threads without external serialization.
    self._lock = threading.Lock()
    self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
    self._conn.execute("PRAGMA journal_mode=WAL")

  def ping(self) -> bool:
    try:
      with self._lock:
        self._conn.execute("SELECT 1")
      return True
    except Exception:
      return False

  def setup(self) -> None:
    with self._lock:
      self._conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audio (
          recording_id TEXT PRIMARY KEY,
          timestamp TEXT,
          document TEXT NOT NULL
        )
        """
      )
      self._conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          timestamp TEXT,
          document TEXT NOT NULL
        )
        """
      )
      self._conn.execute("CREATE INDEX IF NOT EXISTS idx_audio_timestamp ON audio(timestamp)")
      self._conn.execute("CREATE INDEX IF NOT EXISTS idx_metadata_timestamp ON metadata(timestamp)")
      self._conn.commit()

  def index_audio(self, document: dict, doc_id: str) -> None:
    try:
      timestamp = _stringify_timestamp(document.get("timestamp"))
      with self._lock:
        self._conn.execute(
          "INSERT INTO audio (recording_id, timestamp, document) VALUES (?, ?, ?) "
          "ON CONFLICT(recording_id) DO UPDATE SET timestamp=excluded.timestamp, document=excluded.document",
          (doc_id, timestamp, _dumps(document)),
        )
        self._conn.commit()
    except Exception as exc:
      print(f"Failed indexing audio record {doc_id}: {exc}")

  def update_audio(self, doc_id: str, fields: dict) -> None:
    try:
      with self._lock:
        row = self._conn.execute(
          "SELECT document FROM audio WHERE recording_id = ?", (doc_id,)
        ).fetchone()
        existing = json.loads(row[0]) if row else {}
        existing.update(fields)
        timestamp = _stringify_timestamp(existing.get("timestamp"))
        self._conn.execute(
          "INSERT INTO audio (recording_id, timestamp, document) VALUES (?, ?, ?) "
          "ON CONFLICT(recording_id) DO UPDATE SET timestamp=excluded.timestamp, document=excluded.document",
          (doc_id, timestamp, _dumps(existing)),
        )
        self._conn.commit()
    except Exception as exc:
      print(f"Failed update to audio/{doc_id}: {exc}")

  def index_metadata(self, document: dict) -> None:
    try:
      timestamp = _stringify_timestamp(document.get("timestamp"))
      with self._lock:
        self._conn.execute(
          "INSERT INTO metadata (timestamp, document) VALUES (?, ?)",
          (timestamp, _dumps(document)),
        )
        self._conn.commit()
    except Exception as exc:
      print(f"Failed indexing metadata record: {exc}")

  def fetch_audio(self, size: int = 250) -> list[dict]:
    return self._fetch("audio", size)

  def fetch_metadata(self, size: int = 250) -> list[dict]:
    return self._fetch("metadata", size)

  def _fetch(self, table: str, size: int) -> list[dict]:
    try:
      with self._lock:
        rows = self._conn.execute(
          f"SELECT rowid, document FROM {table} ORDER BY timestamp DESC LIMIT ?",
          (size,),
        ).fetchall()
      docs = []
      for rowid, document_json in rows:
        item = json.loads(document_json)
        item.setdefault("_id", str(rowid))
        docs.append(item)
      return docs
    except Exception:
      return []


def create_storage(config: dict) -> Storage:
  backend = str(config.get("storage_backend", "elasticsearch")).strip().lower()

  if backend == "sqlite":
    return SqliteStorage(config.get("sqlite_path", "runtime/bird-analyzer.db"))

  if backend not in ("elasticsearch", "es"):
    print(f"Unknown storage_backend '{backend}', falling back to elasticsearch")

  return ElasticsearchStorage(
    config["elasticsearch_host"],
    config["elasticsearch_user"],
    config["elasticsearch_password"],
    config["cert_loc"],
    config.get("audio_index_name", "bird-audio"),
    config.get("metadata_index_name", "bird-analyzer"),
  )


class ResilientStorage(Storage):
  """Writes locally first and mirrors to an optional remote backend when available."""

  def __init__(self, fallback: SqliteStorage, primary: Storage | None = None):
    self._fallback = fallback
    self._primary = primary

  def _primary_is_available(self) -> bool:
    return self._primary is not None and self._primary.ping()

  def _mirror(self, method_name: str, *args, **kwargs) -> None:
    if not self._primary_is_available():
      return
    try:
      getattr(self._primary, method_name)(*args, **kwargs)
    except Exception as exc:
      print(f"Failed mirroring to configured storage: {exc}")

  def ping(self) -> bool:
    return self._fallback.ping()

  def setup(self) -> None:
    self._fallback.setup()
    self._mirror("setup")

  def index_audio(self, document: dict, doc_id: str) -> None:
    self._fallback.index_audio(document, doc_id)
    self._mirror("index_audio", document, doc_id)

  def update_audio(self, doc_id: str, fields: dict) -> None:
    self._fallback.update_audio(doc_id, fields)
    self._mirror("update_audio", doc_id, fields)

  def index_metadata(self, document: dict) -> None:
    self._fallback.index_metadata(document)
    self._mirror("index_metadata", document)

  def fetch_audio(self, size: int = 250) -> list[dict]:
    return self._fallback.fetch_audio(size)

  def fetch_metadata(self, size: int = 250) -> list[dict]:
    return self._fallback.fetch_metadata(size)


def create_available_storage(config: dict) -> Storage:
  """Return storage that continues operating through remote-backend outages."""
  backend = str(config.get("storage_backend", "elasticsearch")).strip().lower()
  if backend == "sqlite":
    storage = SqliteStorage(config.get("sqlite_path", "runtime/bird-analyzer.db"))
    storage.setup()
    return storage

  fallback = SqliteStorage(config.get("sqlite_path", "runtime/bird-analyzer.db"))
  fallback.setup()
  try:
    primary = create_storage(config)
    if not primary.ping():
      print("Configured storage is unavailable; continuing with local SQLite storage")
    else:
      primary.setup()
  except Exception as exc:
    print(f"Failed to initialize configured storage ({exc}); continuing with local SQLite storage")
    primary = None

  return ResilientStorage(fallback, primary)
