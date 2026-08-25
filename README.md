Homemade Bird Buddy
===================

A real-time bird audio detection and analysis pipeline using BirdNET, Elasticsearch, and a web dashboard.

Architecture
------------

The analyzer runs as a split pipeline with two worker threads:

*   **Recorder thread**: continuously captures 3-second audio chunks and stores them to `recordings/`
*   **Processor thread**: consumes recordings from a queue, runs BirdNET predictions, enriches metadata, and archives high-confidence clips
*   **Runtime heartbeat**: periodically writes `runtime/status.json` so other services can monitor analyzer state

Data Storage
~~~~~~~~~~~~

Two storage backends are supported, selected via `storage_backend` in `config.py`:

*   **Elasticsearch** (default): run `python scripts/setup_elasticsearch.py` to start the bundled `docker-compose.elastic.yml` stack and wait for it to become healthy. Requires Docker.
*   **SQLite**: run `python scripts/setup_sqlite.py` to create the database file and tables. Pure Python/stdlib, no external services — a good fit for low-power devices like a Raspberry Pi.

Either backend stores the same two document types:

*   `bird-audio`: one document per recorded audio file with state (recorded/processed/failed) and acoustic features (pitch, spectral centroid, bandwidth, RMS)
*   `bird-analyzer`: processed species detections with metadata from BirdNET and enrichment API

SQLite Schema
~~~~~~~~~~~~~

The SQLite database path is set by `sqlite_path` (by default,
`runtime/bird-analyzer.db`). It contains two tables:

| Table | Columns | Contents |
| --- | --- | --- |
| `audio` | `recording_id TEXT PRIMARY KEY`, `timestamp TEXT`, `document TEXT NOT NULL` | One row per recording. `document` is JSON containing the recording path, sample rate, duration, processing status, top prediction, confidence, and acoustic features. |
| `metadata` | `id INTEGER PRIMARY KEY AUTOINCREMENT`, `timestamp TEXT`, `document TEXT NOT NULL` | One row per BirdNET detection. `document` is JSON containing the common and scientific names, detection confidence, recording reference, acoustic features, and optional enrichment metadata. |

Both tables have a timestamp index (`idx_audio_timestamp` and
`idx_metadata_timestamp`) for newest-first dashboard queries. The JSON payloads
allow new metadata fields to be stored without a database migration.

Web Dashboard & API
-------------------

A Flask server (`server.py`) runs alongside the analyzer and provides:

*   `GET /api/status` - analyzer heartbeat and running state
*   `GET /api/audio` - audio index documents (`bird-audio`)
*   `GET /api/processed` - metadata rows (`bird-analyzer`)
*   `GET /api/space` - 3D acoustic visualization (pitch, spectral centroid, spectral bandwidth)

The dashboard at `/` displays:

*   all recorded audio events
*   all species detections
*   a 3D plot of bird calls in acoustic space

Configuration
--------------

Settings are loaded from `config.py` (or `config.example.py` as fallback). Required parameters:

*   `storage_backend`: `"elasticsearch"` (default) or `"sqlite"`. Use `"sqlite"` for a self-contained setup with no external service — a good fit for low-power devices like a Raspberry Pi.
*   `sqlite_path`: path to the SQLite database file, used when `storage_backend` is `"sqlite"`
*   `elasticsearch_host`: Elasticsearch server URL (used when `storage_backend` is `"elasticsearch"`)
*   `elasticsearch_user` / `elasticsearch_password`: authentication credentials
*   `cert_loc`: path to Elasticsearch CA certificate
*   `api_key`: API key for bird enrichment service (nuthatch.lastelm.software)
*   `webhook_url`: Discord webhook URL for notifications (optional)
*   `offline_mode`: when `True`, prevents BirdNET model downloads while allowing cached models to load
*   `allow_model_downloads`: defaults to `False`; set to `True` temporarily while connected to download missing BirdNET model assets
*   `enable_online_enrichment`: set to `True` with an `api_key` to request optional Nuthatch metadata
*   `nuthatch_hourly_limit`: defaults to `500`; shared SQLite cache and request ledger enforce this limit across live enrichment and dashboard backfill
*   `audio_device_override`: specific audio device index or name (optional; defaults to first available input device)
*   `location_latitude` / `location_longitude`: geographic coordinates for species predictions

Key Features
~~~~~~~~~~~~

*   Real-time streaming audio recording and analysis
*   Automatic audio device detection
*   BirdNET-based species prediction with configurable confidence threshold
*   Acoustic feature extraction (pitch, spectral analysis, RMS)
*   Data enrichment from external bird API
*   Long-term Elasticsearch storage for analysis and visualization
*   Discord notifications for high-confidence detections
*   Archive storage for significant audio clips
*   Web UI with 3D acoustic visualization

Offline Use
-----------

The analyzer stores its data in SQLite by default and records predictions without
network access. When Elasticsearch is configured, every write is also retained
in local SQLite and mirrored to Elasticsearch whenever it is reachable, so an
intermittent connection does not lose events. Optional metadata enrichment uses
local species information during an outage and retries on a later detection.

Before disconnecting a device, install the Python dependencies and run the
analyzer once while connected so BirdNET can download its acoustic model. The
downloaded model remains in BirdNET's local app-data cache. If that model is
absent while `offline_mode` is enabled, recordings are retained with an
`awaiting_acoustic_model` status instead of being deleted.

Audio Device Setup
------------------

The analyzer requires a working audio input device (microphone). Follow the platform-specific setup below if you encounter audio device issues.

**Linux**

Install PortAudio development libraries:

```bash
sudo apt-get install libportaudio2
```

If that doesn't work, also try installing ALSA (Advanced Linux Sound Architecture):

```bash
sudo apt-get install libasound-dev
```

**macOS**

Audio support is typically built-in. If you encounter issues, ensure your microphone is properly connected and permitted in:

- System Preferences > Security & Privacy > Microphone

You may also need to reinstall dependencies:

```bash
pip install --upgrade sounddevice soundfile
```

**Windows**

Audio support is typically built-in. If you encounter issues:

1. Check Device Manager to ensure your microphone is recognized
2. Ensure your microphone is set as the default input device in Sound Settings
3. Check Windows Privacy Settings (Settings > Privacy > Microphone) and grant app permissions

You can also override the audio device in `config.py` by setting `audio_device_override` to a specific device index.

Installation & Running
----------------------

1. Install audio device libraries (see "Audio Device Setup" section above for your OS)

2. Install Python dependencies:

	`pip install -r requirements.txt`

3. Create `config.py` from `config.example.py` and set your parameters:

	`cp config.example.py config.py`

   Update `config.py` with your API key, coordinates, and other settings.

4. Set up a storage backend:

   *   Elasticsearch (requires Docker): `python scripts/setup_elasticsearch.py`
   *   SQLite (works anywhere, no external services): `python scripts/setup_sqlite.py`

   Make sure `storage_backend` in `config.py` matches the one you set up.

5. Start the analyzer pipeline:

	`python analyze.py`

6. In another terminal, start the web server:

	`python server.py`

7. Open `http://localhost:8080` in your browser

Next Steps
----------

*   Extended multi-day event aggregation in the dashboard
*   Integration with wildlife camera for synchronized video capture
*   Model fine-tuning using archived detections for improved accuracy
