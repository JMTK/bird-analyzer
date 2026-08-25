import os

# Optional override for audio input device. Set to an integer index or exact device name string to force a specific microphone.
# Leave as None to auto-select the first available input device.
audio_device_override = None

webhook_url = ""

# Storage backend for recordings/predictions: "elasticsearch" or "sqlite".
# sqlite is a good fit for low-power devices like a Raspberry Pi since it needs no external service.
storage_backend = "elasticsearch"
sqlite_path = os.path.join(os.getcwd(), "runtime", "bird-analyzer.db")

elasticsearch_host = "https://localhost:9200"
elasticsearch_user = "elastic"
elasticsearch_password = ""
cert_loc = os.path.join(os.getcwd(), "http_ca.crt")

api_key=""

# Latitude and longitude for bird species prediction
location_latitude = 39.731782
location_longitude = -104.991257