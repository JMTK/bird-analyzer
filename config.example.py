import os

# Optional override for audio input device. Set to an integer index or exact device name string to force a specific microphone.
# Leave as None to auto-select the first available input device.
audio_device_override = None

webhook_url = ""

elasticsearch_host = "https://localhost:9200"
elasticsearch_user = "elastic"
elasticsearch_password = ""
cert_loc = os.path.join(os.getcwd(), "http_ca.crt")

api_key=""

# Latitude and longitude for bird species prediction
location_latitude = 39.731782
location_longitude = -104.991257