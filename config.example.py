import os

# Optional override for audio input device. Set to an integer index or exact device name string to force a specific microphone.
# Leave as None to auto-select the first available input device.
audio_device_override = None

webhook_url = ""

# Prevent BirdNET model downloads while still allowing cached models to load.
offline_mode = False
# Enable temporarily while connected to download missing BirdNET model assets.
allow_model_downloads = False
enable_online_enrichment = True
# Shared limit across the analyzer and dashboard backfill.
nuthatch_hourly_limit = 500

# Storage backend for recordings/predictions: "elasticsearch" or "sqlite".
# sqlite is a good fit for low-power devices like a Raspberry Pi since it needs no external service.
storage_backend = "sqlite"
sqlite_path = os.path.join(os.getcwd(), "runtime", "bird-analyzer.db")

# The dashboard is local-only by default. Use an authenticated reverse proxy
# rather than exposing the Flask development server to other devices.
dashboard_host = "127.0.0.1"

elasticsearch_host = "https://localhost:9200"
elasticsearch_user = "elastic"
elasticsearch_password = ""
cert_loc = os.path.join(os.getcwd(), "http_ca.crt")

api_key=""

# BirdNET acoustic prediction settings. Keep workers and batch size low on
# low-memory devices such as a Raspberry Pi.
default_confidence_threshold = 0.1
prediction_workers = 1
prediction_batch_size = 1

# Latitude and longitude for bird species prediction
location_latitude = 39.731782
location_longitude = -104.991257