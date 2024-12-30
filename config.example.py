import os
import sounddevice as sd

sd.default.device = ''

webhook_url = ""

elasticsearch_host = "https://localhost:9200"
elasticsearch_user = "elastic"
elasticsearch_password = ""
cert_loc = os.path.join(os.getcwd(), "http_ca.crt")

api_key=""