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

Elasticsearch indexes store two types of documents:

*   `bird-audio`: one document per recorded audio file with state (recorded/processed/failed) and acoustic features (pitch, spectral centroid, bandwidth, RMS)
*   `bird-analyzer`: processed species detections with metadata from BirdNET and enrichment API

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

*   `elasticsearch_host`: Elasticsearch server URL
*   `elasticsearch_user` / `elasticsearch_password`: authentication credentials
*   `cert_loc`: path to Elasticsearch CA certificate
*   `api_key`: API key for bird enrichment service (nuthatch.lastelm.software)
*   `webhook_url`: Discord webhook URL for notifications (optional)
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

Installation & Running
----------------------

1. Install dependencies:

	`pip install -r requirements.txt`

2. Create `config.py` from `config.example.py` and set your parameters:

	`cp config.example.py config.py`

   Update `config.py` with your Elasticsearch credentials, API key, coordinates, and other settings.

3. Start the analyzer pipeline:

	`python analyze.py`

4. In another terminal, start the web server:

	`python server.py`

5. Open `http://localhost:8080` in your browser

Next Steps
----------

*   Extended multi-day event aggregation in the dashboard
*   Integration with wildlife camera for synchronized video capture
*   Model fine-tuning using archived detections for improved accuracy
