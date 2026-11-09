"""Storage backends for audio/metadata documents.

Provides a common interface so analyze.py and server.py can persist and
query recordings either through Elasticsearch or a local SQLite database,
selected via the `storage_backend` config option.
"""

import datetime
import json
import sqlite3
import threading
import uuid
from pathlib import Path

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

  def update_metadata(self, doc_id: str, fields: dict) -> None:
    raise NotImplementedError

  def get_enrichment_cache(self, cache_key: str) -> dict | None:
    raise NotImplementedError

  def set_enrichment_cache(self, cache_key: str, document: dict) -> None:
    raise NotImplementedError

  def reserve_enrichment_request(self, hourly_limit: int) -> bool:
    raise NotImplementedError

  def fetch_audio(self, size: int = 250) -> list[dict]:
    raise NotImplementedError

  def fetch_pending_audio(self) -> list[dict]:
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
      if not self.client.indices.exists(index=index_name):
        self.client.indices.create(index=index_name)

  def index_audio(self, document: dict, doc_id: str) -> None:
    self.client.index(index=self.audio_index, id=doc_id, document=document)

  def update_audio(self, doc_id: str, fields: dict) -> None:
    self.client.update(index=self.audio_index, id=doc_id, doc=fields, doc_as_upsert=True)

  def index_metadata(self, document: dict) -> None:
    self.client.index(
      index=self.metadata_index,
      id=document.get("_remote_id"),
      document=document,
    )

  def update_metadata(self, doc_id: str, fields: dict) -> None:
    self.client.update(index=self.metadata_index, id=doc_id, doc=fields)

  def get_enrichment_cache(self, cache_key: str) -> dict | None:
    return None

  def set_enrichment_cache(self, cache_key: str, document: dict) -> None:
    return None

  def reserve_enrichment_request(self, hourly_limit: int) -> bool:
    return True

  def fetch_audio(self, size: int = 250) -> list[dict]:
    return self._fetch(self.audio_index, size)

  def fetch_metadata(self, size: int = 250) -> list[dict]:
    return self._fetch(self.metadata_index, size)

  def _fetch(self, index_name: str, size: int) -> list[dict]:
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

  def fetch_pending_audio(self) -> list[dict]:
    response = self.client.search(
      index=self.audio_index,
      size=10000,
      sort=[{"timestamp": {"order": "asc", "unmapped_type": "date"}}],
      query={"terms": {"status.keyword": ["recorded", "awaiting_acoustic_model"]}},
    )
    hits = response.get("hits", {}).get("hits", [])
    docs = []
    for hit in hits:
      item = hit.get("_source", {})
      item["_id"] = hit.get("_id")
      docs.append(item)
    return docs


class SqliteStorage(Storage):
  """Stores documents as JSON blobs, mirroring Elasticsearch's flexible schema."""

  def __init__(self, db_path: str, mirror_enabled: bool = False):
    self.db_path = Path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    # Single shared connection guarded by a lock; sqlite3 connections aren't safe
    # for concurrent use from multiple threads without external serialization.
    self._lock = threading.Lock()
    self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
    self._conn.execute("PRAGMA journal_mode=WAL")
    self._mirror_enabled = mirror_enabled

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
      self._conn.execute(
        """
        CREATE TABLE IF NOT EXISTS enrichment_cache (
          cache_key TEXT PRIMARY KEY,
          updated_at TEXT NOT NULL,
          document TEXT NOT NULL
        )
        """
      )
      self._conn.execute(
        """
        CREATE TABLE IF NOT EXISTS enrichment_requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          requested_at TEXT NOT NULL
        )
        """
      )
      self._conn.execute(
        """
        CREATE TABLE IF NOT EXISTS remote_outbox (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          method TEXT NOT NULL,
          payload TEXT NOT NULL
        )
        """
      )
      self._conn.execute("CREATE INDEX IF NOT EXISTS idx_audio_timestamp ON audio(timestamp)")
      self._conn.execute("CREATE INDEX IF NOT EXISTS idx_metadata_timestamp ON metadata(timestamp)")
      self._conn.execute("CREATE INDEX IF NOT EXISTS idx_enrichment_requests_timestamp ON enrichment_requests(requested_at)")
      self._conn.commit()

  def index_audio(self, document: dict, doc_id: str) -> None:
    timestamp = _stringify_timestamp(document.get("timestamp"))
    with self._lock:
      self._conn.execute(
        "INSERT INTO audio (recording_id, timestamp, document) VALUES (?, ?, ?) "
        "ON CONFLICT(recording_id) DO UPDATE SET timestamp=excluded.timestamp, document=excluded.document",
        (doc_id, timestamp, _dumps(document)),
      )
      if self._mirror_enabled:
        self._enqueue_remote_operation_locked("index_audio", [document, doc_id])
      self._conn.commit()

  def update_audio(self, doc_id: str, fields: dict) -> None:
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
      if self._mirror_enabled:
        self._enqueue_remote_operation_locked("update_audio", [doc_id, fields])
      self._conn.commit()

  def index_metadata(self, document: dict) -> None:
    if self._mirror_enabled and not document.get("_remote_id"):
      document["_remote_id"] = str(uuid.uuid4())
    timestamp = _stringify_timestamp(document.get("timestamp"))
    with self._lock:
      self._conn.execute(
        "INSERT INTO metadata (timestamp, document) VALUES (?, ?)",
        (timestamp, _dumps(document)),
      )
      if self._mirror_enabled:
        self._enqueue_remote_operation_locked("index_metadata", [document])
      self._conn.commit()

  def update_metadata(self, doc_id: str, fields: dict) -> None:
    with self._lock:
      row = self._conn.execute(
        "SELECT document FROM metadata WHERE id = ?", (doc_id,)
      ).fetchone()
      if row is None:
        raise KeyError(f"Metadata record {doc_id} does not exist")
      existing = json.loads(row[0])
      was_mirrored = bool(existing.get("_remote_id"))
      if self._mirror_enabled and not existing.get("_remote_id"):
        existing["_remote_id"] = str(uuid.uuid4())
      existing.update(fields)
      timestamp = _stringify_timestamp(existing.get("timestamp"))
      self._conn.execute(
        "UPDATE metadata SET timestamp = ?, document = ? WHERE id = ?",
        (timestamp, _dumps(existing), doc_id),
      )
      if self._mirror_enabled:
        if was_mirrored:
          self._enqueue_remote_operation_locked(
            "update_metadata",
            [existing["_remote_id"], fields],
          )
        else:
          self._enqueue_remote_operation_locked("index_metadata", [existing])
      self._conn.commit()

  def _enqueue_remote_operation_locked(self, method: str, args: list) -> None:
    self._conn.execute(
      "INSERT INTO remote_outbox (method, payload) VALUES (?, ?)",
      (method, _dumps({"args": args})),
    )

  def pending_remote_operations(self) -> list[tuple[int, str, dict]]:
    with self._lock:
      rows = self._conn.execute(
        "SELECT id, method, payload FROM remote_outbox ORDER BY id"
      ).fetchall()
    return [(row_id, method, json.loads(payload)) for row_id, method, payload in rows]

  def delete_remote_operation(self, operation_id: int) -> None:
    with self._lock:
      self._conn.execute("DELETE FROM remote_outbox WHERE id = ?", (operation_id,))
      self._conn.commit()

  def get_enrichment_cache(self, cache_key: str) -> dict | None:
    try:
      with self._lock:
        row = self._conn.execute(
          "SELECT document FROM enrichment_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
      return json.loads(row[0]) if row else None
    except Exception as exc:
      print(f"Failed reading enrichment cache for {cache_key}: {exc}")
      return None

  def set_enrichment_cache(self, cache_key: str, document: dict) -> None:
    try:
      with self._lock:
        self._conn.execute(
          "INSERT INTO enrichment_cache (cache_key, updated_at, document) VALUES (?, ?, ?) "
          "ON CONFLICT(cache_key) DO UPDATE SET updated_at=excluded.updated_at, document=excluded.document",
          (cache_key, datetime.datetime.now(datetime.timezone.utc).isoformat(), _dumps(document)),
        )
        self._conn.commit()
    except Exception as exc:
      print(f"Failed writing enrichment cache for {cache_key}: {exc}")

  def reserve_enrichment_request(self, hourly_limit: int) -> bool:
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat()
    requested_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
      with self._lock:
        recent_count = self._conn.execute(
          "SELECT COUNT(*) FROM enrichment_requests WHERE requested_at >= ?", (cutoff,)
        ).fetchone()[0]
        if recent_count >= hourly_limit:
          return False
        self._conn.execute(
          "INSERT INTO enrichment_requests (requested_at) VALUES (?)", (requested_at,)
        )
        self._conn.commit()
      return True
    except Exception as exc:
      print(f"Failed reserving enrichment request: {exc}")
      return False

  def fetch_audio(self, size: int = 250) -> list[dict]:
    return self._fetch("audio", size)

  def fetch_pending_audio(self) -> list[dict]:
    with self._lock:
      rows = self._conn.execute(
        "SELECT rowid, document FROM audio ORDER BY timestamp ASC"
      ).fetchall()
    docs = []
    for rowid, document_json in rows:
      item = json.loads(document_json)
      item.setdefault("_id", str(rowid))
      if item.get("status") in ("recorded", "awaiting_acoustic_model"):
        docs.append(item)
    return docs

  def fetch_metadata(self, size: int = 250) -> list[dict]:
    return self._fetch("metadata", size)

  def _fetch(self, table: str, size: int) -> list[dict]:
    if table not in ("audio", "metadata"):
      raise ValueError(f"Unsupported storage table: {table}")
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


def create_storage(config: dict) -> Storage:
  backend = str(config.get("storage_backend", "sqlite")).strip().lower()

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
    self._sync_lock = threading.Lock()
    self._remote_setup = False
    self._sync_thread_started = False

  def sync_pending(self) -> int:
    if self._primary is None or not self._primary.ping():
      return 0

    synced = 0
    with self._sync_lock:
      try:
        if not self._remote_setup:
          self._primary.setup()
          self._remote_setup = True
        for operation_id, method, payload in self._fallback.pending_remote_operations():
          getattr(self._primary, method)(*payload["args"])
          self._fallback.delete_remote_operation(operation_id)
          synced += 1
      except Exception as exc:
        print(f"Failed syncing to configured storage; pending changes retained locally: {exc}")
    return synced

  def start_sync_thread(self) -> None:
    if self._sync_thread_started:
      return
    self._sync_thread_started = True

    def sync_loop() -> None:
      while True:
        threading.Event().wait(30)
        self.sync_pending()

    threading.Thread(target=sync_loop, daemon=True).start()

  def ping(self) -> bool:
    return self._fallback.ping()

  def setup(self) -> None:
    self._fallback.setup()
    self.sync_pending()

  def index_audio(self, document: dict, doc_id: str) -> None:
    self._fallback.index_audio(document, doc_id)
    self.sync_pending()

  def update_audio(self, doc_id: str, fields: dict) -> None:
    self._fallback.update_audio(doc_id, fields)
    self.sync_pending()

  def index_metadata(self, document: dict) -> None:
    self._fallback.index_metadata(document)
    self.sync_pending()

  def update_metadata(self, doc_id: str, fields: dict) -> None:
    self._fallback.update_metadata(doc_id, fields)
    self.sync_pending()

  def get_enrichment_cache(self, cache_key: str) -> dict | None:
    return self._fallback.get_enrichment_cache(cache_key)

  def set_enrichment_cache(self, cache_key: str, document: dict) -> None:
    self._fallback.set_enrichment_cache(cache_key, document)

  def reserve_enrichment_request(self, hourly_limit: int) -> bool:
    return self._fallback.reserve_enrichment_request(hourly_limit)

  def fetch_audio(self, size: int = 250) -> list[dict]:
    return self._fallback.fetch_audio(size)

  def fetch_pending_audio(self) -> list[dict]:
    return self._fallback.fetch_pending_audio()

  def fetch_metadata(self, size: int = 250) -> list[dict]:
    return self._fallback.fetch_metadata(size)


def create_available_storage(config: dict) -> Storage:
  """Return storage that continues operating through remote-backend outages."""
  backend = str(config.get("storage_backend", "sqlite")).strip().lower()
  if backend == "sqlite":
    storage = SqliteStorage(config.get("sqlite_path", "runtime/bird-analyzer.db"))
    storage.setup()
    return storage

  fallback = SqliteStorage(config.get("sqlite_path", "runtime/bird-analyzer.db"), mirror_enabled=True)
  fallback.setup()
  try:
    primary = create_storage(config)
    if not primary.ping():
      print("Configured storage is unavailable; continuing with local SQLite storage")
  except Exception as exc:
    print(f"Failed to initialize configured storage ({exc}); continuing with local SQLite storage")
    primary = None

  storage = ResilientStorage(fallback, primary)
  storage.sync_pending()
  storage.start_sync_thread()
  return storage
