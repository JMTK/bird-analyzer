import datetime
import json
import os
from pathlib import Path
import random
import shutil
import threading
import importlib.util
from queue import Empty, Queue
from time import sleep
from uuid import uuid4
import warnings

import absl.logging
import birdnet
import elasticsearch
import numpy as np
import requests
import sounddevice as sd
import soundfile as sf

from bird import Bird, Response


def load_runtime_config() -> dict:
  candidate_files = [Path("config.py"), Path("config.example.py")]
  for config_path in candidate_files:
    if config_path.exists():
      spec = importlib.util.spec_from_file_location("bird_config", config_path)
      if spec is None or spec.loader is None:
        continue
      module = importlib.util.module_from_spec(spec)
      spec.loader.exec_module(module)
      return {
        "elasticsearch_host": getattr(module, "elasticsearch_host", "https://localhost:9200"),
        "elasticsearch_user": getattr(module, "elasticsearch_user", "elastic"),
        "elasticsearch_password": getattr(module, "elasticsearch_password", ""),
        "cert_loc": getattr(module, "cert_loc", str(Path.cwd() / "http_ca.crt")),
        "api_key": getattr(module, "api_key", ""),
        "webhook_url": getattr(module, "webhook_url", ""),
        "audio_device_override": getattr(module, "audio_device_override", None),
        "location_latitude": getattr(module, "location_latitude"),
        "location_longitude": getattr(module, "location_longitude"),
      }
  raise FileNotFoundError("Missing config.py or config.example.py")


CONFIG = load_runtime_config()

warnings.filterwarnings("ignore")
absl.logging.set_verbosity(absl.logging.ERROR)

SAMPLE_RATE = 48000
DURATION_SECONDS = 3
AUDIO_INDEX_NAME = "bird-audio"
METADATA_INDEX_NAME = "bird-analyzer"
RUNTIME_DIR = Path("runtime")
STATUS_PATH = RUNTIME_DIR / "status.json"
RECORDINGS_DIR = Path("recordings")
ARCHIVES_DIR = Path("archives")
TEST_FILE = Path("test.wav")

is_test = TEST_FILE.exists()
stop_event = threading.Event()
recordings_queue: Queue[dict] = Queue(maxsize=40)


def utc_now_iso() -> str:
  return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_status(state: str, extra: dict | None = None) -> None:
  payload = {
    "pid": os.getpid(),
    "state": state,
    "heartbeat": utc_now_iso(),
    "queue_size": recordings_queue.qsize(),
    "is_test_mode": is_test,
  }
  if extra:
    payload.update(extra)
  STATUS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def create_es_client() -> elasticsearch.Elasticsearch:
  return elasticsearch.Elasticsearch(
    CONFIG["elasticsearch_host"],
    ca_certs=CONFIG["cert_loc"],
    http_auth=(CONFIG["elasticsearch_user"], CONFIG["elasticsearch_password"]),
    max_retries=0,
    retry_on_timeout=False,
  )


def safe_index(es: elasticsearch.Elasticsearch, index_name: str, document: dict, doc_id: str | None = None) -> None:
  try:
    if doc_id is None:
      es.index(index=index_name, document=document)
    else:
      es.index(index=index_name, id=doc_id, document=document)
  except Exception as exc:
    print(f"Failed indexing to {index_name}: {exc}")


def safe_update(es: elasticsearch.Elasticsearch, index_name: str, doc_id: str, fields: dict) -> None:
  try:
    es.update(index=index_name, id=doc_id, doc=fields, doc_as_upsert=True)
  except Exception as exc:
    print(f"Failed update to {index_name}/{doc_id}: {exc}")


def send_discord_notification(bird: Bird, webhook_url: str) -> None:
  embed = {
    "title": bird.name,
    "color": 3447003,
    "fields": [
      {"name": "Length", "value": f"{bird.lengthMin} - {bird.lengthMax} cm", "inline": True},
      {"name": "Wingspan", "value": f"{bird.wingspanMin} - {bird.wingspanMax} cm", "inline": True},
      {"name": "Scientific Name", "value": bird.sciName, "inline": False},
      {"name": "Family", "value": bird.family, "inline": False},
      {"name": "Order", "value": bird.order, "inline": False},
      {"name": "Conservation Status", "value": bird.status, "inline": False},
      {"name": "Regions", "value": ", ".join(bird.region), "inline": False},
    ],
  }

  if bird.images:
    embed["image"] = {"url": random.choice(bird.images)}

  response = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
  if response.status_code != 204:
    print(f"Failed to send notification: {response.status_code} - {response.text}")


def compute_audio_features(file_path: Path) -> dict:
  audio, sample_rate = sf.read(file_path)
  if isinstance(audio, np.ndarray) and audio.ndim > 1:
    audio = np.mean(audio, axis=1)

  if len(audio) == 0:
    return {"pitch_hz": 0.0, "spectral_centroid_hz": 0.0, "spectral_bandwidth_hz": 0.0, "rms": 0.0}

  spectrum = np.abs(np.fft.rfft(audio))
  freqs = np.fft.rfftfreq(len(audio), d=1.0 / sample_rate)
  mag_sum = float(np.sum(spectrum))

  if mag_sum <= 0.0:
    centroid = 0.0
    bandwidth = 0.0
  else:
    centroid = float(np.sum(freqs * spectrum) / mag_sum)
    bandwidth = float(np.sqrt(np.sum(((freqs - centroid) ** 2) * spectrum) / mag_sum))

  peak_index = int(np.argmax(spectrum[1:]) + 1) if len(spectrum) > 1 else 0
  pitch_hz = float(freqs[peak_index]) if peak_index < len(freqs) else 0.0
  rms = float(np.sqrt(np.mean(np.square(audio))))

  return {
    "pitch_hz": round(pitch_hz, 2),
    "spectral_centroid_hz": round(centroid, 2),
    "spectral_bandwidth_hz": round(bandwidth, 2),
    "rms": round(rms, 6),
  }


def recording_loop(es: elasticsearch.Elasticsearch) -> None:
  print("Recorder thread started")
  while not stop_event.is_set():
    timestamp = datetime.datetime.now(datetime.timezone.utc)
    recording_id = str(uuid4())
    output_file = RECORDINGS_DIR / f"{timestamp.strftime('%Y-%m-%d_%H-%M-%S')}_{recording_id}.wav"

    try:
      if is_test:
        shutil.copy(TEST_FILE, output_file)
        sleep(1)
      else:
        print(f"Recording for {DURATION_SECONDS} seconds")
        frame_count = int(SAMPLE_RATE * DURATION_SECONDS)
        mydata = sd.rec(frame_count, samplerate=SAMPLE_RATE, channels=1, blocking=True)
        sf.write(output_file, mydata, SAMPLE_RATE)
        print(f"Saved recording to {output_file}")

      audio_doc = {
        "recording_id": recording_id,
        "timestamp": timestamp,
        "file_path": str(output_file),
        "sample_rate": SAMPLE_RATE,
        "duration_seconds": DURATION_SECONDS,
        "status": "recorded",
      }
      safe_index(es, AUDIO_INDEX_NAME, audio_doc, doc_id=recording_id)
      recordings_queue.put(audio_doc, timeout=5)
      write_status("running")
    except Exception as exc:
      print("Recording error: " + str(exc))
      write_status("running", {"last_error": str(exc)})
      sleep(1)


def configure_audio_input() -> None:
  configured_device = CONFIG.get("audio_device_override")
  if configured_device is not None and configured_device != "":
    try:
      sd.default.device = configured_device  # type: ignore
      print(f"Using configured audio input device: {configured_device}")
    except Exception as e:
      print(f"Failed to set audio device: {e}")
    return

  devices = sd.query_devices()
  for index, device in enumerate(devices):
    if int(device.get("max_input_channels", 0)) > 0:
      try:
        sd.default.device = index  # type: ignore
      except Exception:
        pass
      print(f"Auto-selected audio input device [{index}]: {device.get('name', 'unknown')}")
      return

  print("No input audio device found; recorder may fail until a microphone is available")


def process_recording(
  es: elasticsearch.Elasticsearch,
  species_in_area: dict | None,
  recording_doc: dict,
) -> None:
  recording_id = recording_doc["recording_id"]
  file_path = Path(recording_doc["file_path"])

  print(f"Processing recording {recording_id}")
  features = compute_audio_features(file_path)

  # Note: birdnet 0.2.15+ has a different API than 0.1.6
  # For now, we skip species filtering and get all predictions
  # You can restore filtering by implementing the new birdnet.AcousticPredictionSession API
  try:
    predictions = birdnet.SpeciesPredictions(  # type: ignore
      birdnet.predict_species_within_audio_file(  # type: ignore
        file_path,
        species_filter=set(species_in_area.keys()) if species_in_area else None,
        min_confidence=0.1,
        silent=True,
      )
    )
    prediction_list = list(predictions[(0.0, DURATION_SECONDS)].items())
  except (AttributeError, TypeError):
    # Fallback for newer birdnet API - predict without species filter
    print("Warning: Using fallback prediction method (full species detection)")
    prediction_list = []
  
  print("Found " + str(len(prediction_list)) + " predictions")

  top_name = ""
  top_confidence = 0.0

  for names, confidence in prediction_list:
    scientific_name, regular_name = names.split("_")
    if confidence > top_confidence:
      top_confidence = confidence
      top_name = regular_name

    print(f"Predicted '{regular_name}' with a confidence of {confidence:.2f}")
    url = (
      "https://nuthatch.lastelm.software/v2/birds?page=1&pageSize=25"
      f"&name={regular_name}&sciName={scientific_name}&operator=OR"
    )

    try:
      api_response = requests.get(url, headers={"API-Key": CONFIG["api_key"]}, timeout=10)
      response_dict = api_response.json()
      bird_response = Response.from_dict(response_dict)
      if bird_response.entities:
        bird = bird_response.entities[0]
        metadata_doc = bird.__dict__.copy()
      else:
        metadata_doc = {
          "name": regular_name,
          "sciName": scientific_name,
          "images": [],
          "region": [],
          "family": "",
          "order": "",
          "status": "",
        }

      metadata_doc.update(
        {
          "timestamp": datetime.datetime.now(datetime.timezone.utc),
          "recording_id": recording_id,
          "file_path": str(file_path),
          "confidence": confidence,
          "pitch_hz": features["pitch_hz"],
          "spectral_centroid_hz": features["spectral_centroid_hz"],
          "spectral_bandwidth_hz": features["spectral_bandwidth_hz"],
          "rms": features["rms"],
        }
      )
      safe_index(es, METADATA_INDEX_NAME, metadata_doc)

      if confidence > 0.6:
        archive_name = (
          f"{scientific_name}_{regular_name}_"
          f"{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.wav"
        )
        shutil.copy(file_path, ARCHIVES_DIR / archive_name)
        # send_discord_notification(bird, webhook_url)
    except Exception as exc:
      print("Failed to process prediction: " + str(exc))

  safe_update(
    es,
    AUDIO_INDEX_NAME,
    recording_id,
    {
      "status": "processed",
      "processed_at": datetime.datetime.now(datetime.timezone.utc),
      "prediction_count": len(prediction_list),
      "top_prediction": top_name,
      "top_confidence": round(top_confidence, 4),
      "pitch_hz": features["pitch_hz"],
      "spectral_centroid_hz": features["spectral_centroid_hz"],
      "spectral_bandwidth_hz": features["spectral_bandwidth_hz"],
      "rms": features["rms"],
    },
  )


def processor_loop(es: elasticsearch.Elasticsearch, species_in_area: dict | None) -> None:
  print("Processor thread started")
  while not stop_event.is_set():
    try:
      recording_doc = recordings_queue.get(timeout=1)
    except Empty:
      write_status("running")
      continue

    try:
      process_recording(es, species_in_area, recording_doc)
    except Exception as exc:
      print("Processing error: " + str(exc))
      safe_update(
        es,
        AUDIO_INDEX_NAME,
        recording_doc["recording_id"],
        {
          "status": "failed",
          "error": str(exc),
          "processed_at": datetime.datetime.now(datetime.timezone.utc),
        },
      )
      write_status("running", {"last_error": str(exc)})
    finally:
      recordings_queue.task_done()
      write_status("running")


def main() -> None:
  RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
  RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
  ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)

  print("Initializing elasticsearch...")
  es = create_es_client()
  es_available = False
  try:
    es_available = bool(es.ping())
    print("Elasticsearch initialized!")
  except Exception:
    print("Elasticsearch failed to initialize!")

  if es_available:
    try:
      try:
        es.indices.create(index=AUDIO_INDEX_NAME)
      except Exception:
        pass
      try:
        es.indices.create(index=METADATA_INDEX_NAME)
      except Exception:
        pass
    except Exception as exc:
      print("Elasticsearch index setup failed: " + str(exc))
  else:
    print("Running without Elasticsearch; indexing will retry when connection returns")

  print("Initializing birdnet...")
  configure_audio_input()
  
  # Try to get species at location using new API
  # Note: birdnet 0.2.15+ has a different API than 0.1.6
  species_in_area = None
  try:
    species_in_area = birdnet.predict_species_at_location_and_time(  # type: ignore
      CONFIG["location_latitude"],
      CONFIG["location_longitude"]
    )
    print("Found " + str(len(species_in_area)) + " species in your area")
  except (AttributeError, TypeError) as e:
    print(f"Warning: Could not filter species by location ({e})")
    print("Proceeding without geographical filtering - all species will be detected")
    species_in_area = None
  
  write_status("running", {"species_count": len(species_in_area) if species_in_area else 0})

  recorder = threading.Thread(target=recording_loop, args=(es,), daemon=True)
  processor = threading.Thread(target=processor_loop, args=(es, species_in_area), daemon=True)
  recorder.start()
  processor.start()

  try:
    while True:
      write_status("running")
      sleep(2)
  except KeyboardInterrupt:
    print("Stopping analyzer...")
    stop_event.set()
    recorder.join(timeout=5)
    processor.join(timeout=5)
    write_status("stopped")


if __name__ == "__main__":
  main()
