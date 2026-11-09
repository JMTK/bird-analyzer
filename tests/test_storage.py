import json
import tempfile
import unittest
from pathlib import Path

from storage import ResilientStorage, SqliteStorage, Storage


class FakeRemoteStorage(Storage):
  def __init__(self):
    self.available = False
    self.documents = {}
    self.fail_next_write = False
    self.setup_count = 0

  def ping(self):
    return self.available

  def setup(self):
    self.setup_count += 1

  def index_audio(self, document, doc_id):
    self._write()
    self.documents[("audio", doc_id)] = document.copy()

  def update_audio(self, doc_id, fields):
    self._write()
    self.documents.setdefault(("audio", doc_id), {}).update(fields)

  def index_metadata(self, document):
    self._write()
    self.documents[("metadata", document["_remote_id"])] = document.copy()

  def update_metadata(self, doc_id, fields):
    self._write()
    self.documents[("metadata", doc_id)].update(fields)

  def _write(self):
    if self.fail_next_write:
      self.fail_next_write = False
      raise RuntimeError("temporary remote failure")


class ResilientStorageTests(unittest.TestCase):
  def setUp(self):
    self.temp_dir = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp_dir.cleanup)
    self.sqlite = SqliteStorage(
      str(Path(self.temp_dir.name) / "test.db"),
      mirror_enabled=True,
    )
    self.sqlite.setup()
    self.addCleanup(self.sqlite._conn.close)
    self.remote = FakeRemoteStorage()
    self.storage = ResilientStorage(self.sqlite, self.remote)

  def test_replays_queued_audio_and_metadata_after_reconnection(self):
    self.storage.index_audio({"status": "recorded"}, "recording-1")
    self.storage.update_audio("recording-1", {"status": "processed"})
    self.storage.index_metadata({"name": "Robin"})
    metadata_id = self.storage.fetch_metadata()[0]["_id"]
    self.storage.update_metadata(metadata_id, {"family": "Turdidae"})

    self.assertEqual(len(self.sqlite.pending_remote_operations()), 4)
    self.remote.available = True

    self.assertEqual(self.storage.sync_pending(), 4)
    self.assertEqual(self.sqlite.pending_remote_operations(), [])
    self.assertEqual(
      self.remote.documents[("audio", "recording-1")]["status"],
      "processed",
    )
    metadata = next(
      document
      for (kind, _), document in self.remote.documents.items()
      if kind == "metadata"
    )
    self.assertEqual(metadata["family"], "Turdidae")
    self.assertEqual(self.remote.setup_count, 1)

  def test_retains_failed_operation_for_retry(self):
    self.remote.available = True
    self.remote.fail_next_write = True
    self.storage.index_audio({"status": "recorded"}, "recording-2")

    self.assertEqual(len(self.sqlite.pending_remote_operations()), 1)
    self.assertEqual(self.storage.sync_pending(), 1)
    self.assertEqual(self.sqlite.pending_remote_operations(), [])
    self.assertIn(("audio", "recording-2"), self.remote.documents)

  def test_enrichment_of_legacy_metadata_can_be_mirrored(self):
    legacy_document = {"name": "Robin", "confidence": 0.9}
    self.sqlite._conn.execute(
      "INSERT INTO metadata (timestamp, document) VALUES (?, ?)",
      ("2026-01-01T00:00:00+00:00", json.dumps(legacy_document)),
    )
    self.sqlite._conn.commit()
    self.remote.available = True

    self.storage.update_metadata("1", {"family": "Turdidae"})

    self.assertEqual(self.sqlite.pending_remote_operations(), [])
    metadata = next(iter(self.remote.documents.values()))
    self.assertEqual(metadata["name"], "Robin")
    self.assertEqual(metadata["family"], "Turdidae")

  def test_fetch_pending_audio_only_returns_resumable_statuses(self):
    for recording_id, status in (
      ("one", "recorded"),
      ("two", "awaiting_acoustic_model"),
      ("three", "processed"),
      ("four", "failed"),
    ):
      self.sqlite.index_audio(
        {"recording_id": recording_id, "status": status},
        recording_id,
      )

    self.assertEqual(
      [document["recording_id"] for document in self.sqlite.fetch_pending_audio()],
      ["one", "two"],
    )

  def test_fetch_errors_are_not_reported_as_empty_results(self):
    self.sqlite._conn.close()

    with self.assertRaises(Exception):
      self.sqlite.fetch_audio()


if __name__ == "__main__":
  unittest.main()
