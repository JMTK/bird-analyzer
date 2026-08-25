import datetime
import json
import os
from pathlib import Path
import random
import shutil
import threading
import importlib.util
from queue import Empty, Full, Queue
from time import sleep
from uuid import uuid4
import warnings

# Cap numeric library thread pools before importing them so model inference can't
# saturate every core and starve the real-time audio capture thread (e.g. on a Raspberry Pi).
for _thread_env_var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "ORT_NUM_THREADS"):
  os.environ.setdefault(_thread_env_var, "2")

import absl.logging
import birdnet
import numpy as np
import requests
import sounddevice as sd
import soundfile as sf
from birdnet.acoustic.models.v3_0.onnx import AcousticOnnxDownloaderV3_0
from birdnet.geo.models.v3_0.onnx import GeoOnnxDownloaderV3_0

from bird import Bird, Response
from storage import Storage, create_available_storage


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
        "storage_backend": getattr(module, "storage_backend", "elasticsearch"),
        "sqlite_path": getattr(module, "sqlite_path", str(Path.cwd() / "runtime" / "bird-analyzer.db")),
        "api_key": getattr(module, "api_key", ""),
        "webhook_url": getattr(module, "webhook_url", ""),
        "audio_device_override": getattr(module, "audio_device_override", None),
        "default_confidence_threshold": getattr(module, "default_confidence_threshold", 0.1),
        "prediction_workers": getattr(module, "prediction_workers", 1),
        "prediction_batch_size": getattr(module, "prediction_batch_size", 1),
        "offline_mode": getattr(module, "offline_mode", True),
        "allow_model_downloads": getattr(module, "allow_model_downloads", False),
        "enable_online_enrichment": getattr(module, "enable_online_enrichment", False),
        "location_latitude": getattr(module, "location_latitude"),
        "location_longitude": getattr(module, "location_longitude"),
      }
  raise FileNotFoundError("Missing config.py or config.example.py")


CONFIG = load_runtime_config()


def acoustic_model_is_available_offline() -> bool:
  return AcousticOnnxDownloaderV3_0._check_acoustic_model_available("fp32")


def geo_model_is_available_offline() -> bool:
  return GeoOnnxDownloaderV3_0._check_geo_model_available("fp32")


def can_download_models() -> bool:
  return CONFIG["allow_model_downloads"] and not CONFIG["offline_mode"]


# Load birdnet models
print("Pre-loading birdnet models...")
try:
  if not acoustic_model_is_available_offline() and not can_download_models():
    print("Acoustic model is not cached; predictions will wait until it is installed locally")
    ACOUSTIC_MODEL = None
  else:
    ACOUSTIC_MODEL = birdnet.load("acoustic", "3.0", "onnx")  # type: ignore
except Exception:
  ACOUSTIC_MODEL = None

GEO_MODEL = None
geo_model_error = ""
if geo_model_is_available_offline() or can_download_models():
  try:
    GEO_MODEL = birdnet.load("geo", "3.0", "onnx")  # type: ignore
  except Exception as exc:
    geo_model_error = str(exc)
else:
  geo_model_error = "model is not cached locally"

warnings.filterwarnings("ignore")
absl.logging.set_verbosity(absl.logging.ERROR)

SAMPLE_RATE = 48000
DURATION_SECONDS = 3
RUNTIME_DIR = Path("runtime")
STATUS_PATH = RUNTIME_DIR / "status.json"
RECORDINGS_DIR = Path("recordings")
ARCHIVES_DIR = Path("archives")
TEST_FILE = Path("test.wav")

is_test = TEST_FILE.exists()
stop_event = threading.Event()
recordings_queue: Queue[dict] = Queue(maxsize=40)
selected_input_device: int | str | None = None

# Reused across requests to avoid re-negotiating TCP/TLS for every prediction lookup.
HTTP_SESSION = requests.Session()
# Processor thread is single-threaded, so a plain dict is safe without locking.
species_metadata_cache: dict[str, dict] = {}

# Recorder, processor, and main threads all call write_status concurrently;
# guard the file write and throttle frequency to limit SD-card wear on devices like a Raspberry Pi.
STATUS_LOCK = threading.Lock()
STATUS_MIN_INTERVAL_SECONDS = 1.0
_last_status_write = 0.0


def utc_now_iso() -> str:
  return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_status(state: str, extra: dict | None = None, force: bool = False) -> None:
  global _last_status_write

  now = datetime.datetime.now().timestamp()
  with STATUS_LOCK:
    if not force and (now - _last_status_write) < STATUS_MIN_INTERVAL_SECONDS:
      return
    _last_status_write = now

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


def recording_loop(storage: Storage) -> None:
  global selected_input_device

  print("Recorder thread started")
  while not stop_event.is_set():
    # Throttle recording before capturing audio so a slow processor thread can catch up
    # instead of piling up unprocessed WAV files on disk.
    if recordings_queue.full():
      write_status("running", {"last_error": "Processing queue full; pausing recording"})
      sleep(1)
      continue

    timestamp = datetime.datetime.now(datetime.timezone.utc)
    recording_id = str(uuid4())
    output_file = RECORDINGS_DIR / f"{timestamp.strftime('%Y-%m-%d_%H-%M-%S')}_{recording_id}.wav"

    try:
      if is_test:
        shutil.copy(TEST_FILE, output_file)
        sleep(1)
      else:
        if not configure_audio_input():
          print("No compatible input audio device found; waiting for a microphone")
          write_status("running", {"last_error": "No compatible input audio device"})
          sleep(2)
          continue
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
      storage.index_audio(audio_doc, doc_id=recording_id)
      try:
        recordings_queue.put(audio_doc, timeout=5)
      except Full:
        # Processor didn't catch up in time; drop the file instead of leaking disk space.
        print(f"Processing queue still full after wait; discarding {output_file}")
        storage.update_audio(recording_id, {"status": "dropped", "error": "queue full"})
        output_file.unlink(missing_ok=True)
      write_status("running")
    except Exception as exc:
      print("Recording error: " + str(exc))
      # Force a fresh device probe next iteration; the cached device may be
      # busy/unavailable (e.g. transient ALSA errors) even though it last worked.
      selected_input_device = None
      write_status("running", {"last_error": str(exc)})
      sleep(1)


def configure_audio_input() -> bool:
  global selected_input_device

  configured_device = CONFIG.get("audio_device_override")
  if configured_device is not None and configured_device != "":
    try:
      sd.check_input_settings(device=configured_device, samplerate=SAMPLE_RATE, channels=1)
      sd.default.device = configured_device  # type: ignore
      if selected_input_device != configured_device:
        print(f"Using configured audio input device: {configured_device}")
        selected_input_device = configured_device
      return True
    except Exception as e:
      print(f"Configured audio device is unavailable: {e}")

  devices = sd.query_devices()
  for index, device in enumerate(devices):
    if int(device.get("max_input_channels", 0)) > 0:
      try:
        sd.check_input_settings(device=index, samplerate=SAMPLE_RATE, channels=1)
        sd.default.device = index  # type: ignore
        if selected_input_device != index:
          print(f"Auto-selected audio input device [{index}]: {device.get('name', 'unknown')}")
          selected_input_device = index
        return True
      except Exception:
        continue

  selected_input_device = None
  return False


def process_recording(
  storage: Storage,
  species_in_area: dict | None,
  recording_doc: dict,
) -> None:
  recording_id = recording_doc["recording_id"]
  file_path = Path(recording_doc["file_path"])

  print(f"Processing recording {recording_id}")
  features = compute_audio_features(file_path)

  if ACOUSTIC_MODEL is None:
    storage.update_audio(
      recording_id,
      {
        "status": "awaiting_acoustic_model",
        "error": "BirdNET acoustic model is not available locally",
        "processed_at": datetime.datetime.now(datetime.timezone.utc),
      },
    )
    print("Prediction skipped: BirdNET acoustic model is unavailable")
    return

  # Use the new birdnet 1.1.0 API
  try:
    # Single worker/batch to cap peak memory; birdnet otherwise spawns one worker
    # per CPU core (each holding a full model copy), which OOM-kills on low-memory devices.
    predictions_result = ACOUSTIC_MODEL.predict(  # type: ignore
      str(file_path),
      default_confidence_threshold=CONFIG["default_confidence_threshold"],
      n_workers=CONFIG["prediction_workers"],
      batch_size=CONFIG["prediction_batch_size"],
    )
    prediction_list = [
      (str(row["species_name"]), float(row["confidence"]))
      for row in predictions_result.to_structured_array()
    ]
  except Exception as e:
    print(f"Prediction error: {e}")
    prediction_list = []
  
  print("Found " + str(len(prediction_list)) + " predictions")

  top_name = ""
  top_confidence = 0.0

  for species_name, confidence in prediction_list:
    # Species name format is "scientific_name_regular_name"
    if "_" in species_name:
      parts = species_name.rsplit("_", 1)
      scientific_name = parts[0]
      regular_name = parts[1]
    else:
      scientific_name = species_name
      regular_name = species_name
    
    if confidence > top_confidence:
      top_confidence = confidence
      top_name = regular_name

    print(f"Predicted '{regular_name}' with a confidence of {confidence:.2f}")

    try:
      cache_key = f"{scientific_name}|{regular_name}"
      cached_metadata = species_metadata_cache.get(cache_key)
      if cached_metadata is None:
        cached_metadata = {
          "name": regular_name,
          "sciName": scientific_name,
          "images": [],
          "region": [],
          "family": "",
          "order": "",
          "status": "",
        }
        cache_metadata = True
        if CONFIG["enable_online_enrichment"] and CONFIG["api_key"]:
          try:
            url = (
              "https://nuthatch.lastelm.software/v2/birds?page=1&pageSize=25"
              f"&name={regular_name}&sciName={scientific_name}&operator=OR"
            )
            api_response = HTTP_SESSION.get(
              url, headers={"API-Key": CONFIG["api_key"]}, timeout=3
            )
            api_response.raise_for_status()
            bird_response = Response.from_dict(api_response.json())
            if bird_response.entities:
              cached_metadata = bird_response.entities[0].__dict__.copy()
          except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"Metadata enrichment unavailable for '{regular_name}': {exc}")
            # Keep local metadata for this detection, but retry enrichment after
            # a transient connectivity failure on a future detection.
            cache_metadata = False
        if cache_metadata:
          species_metadata_cache[cache_key] = cached_metadata

      metadata_doc = cached_metadata.copy()
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
      storage.index_metadata(metadata_doc)

      if confidence > 0.6:
        archive_name = (
          f"{scientific_name}_{regular_name}_"
          f"{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.wav"
        )
        shutil.copy(file_path, ARCHIVES_DIR / archive_name)
        # send_discord_notification(bird, webhook_url)
    except Exception as exc:
      print("Failed to process prediction: " + str(exc))

  storage.update_audio(
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

  if not prediction_list:
    try:
      file_path.unlink()
      print(f"Deleted recording with no predictions: {file_path}")
    except OSError as exc:
      print(f"Failed to delete recording with no predictions: {exc}")


def processor_loop(storage: Storage, species_in_area: dict | None) -> None:
  print("Processor thread started")
  while not stop_event.is_set():
    try:
      recording_doc = recordings_queue.get(timeout=1)
    except Empty:
      write_status("running")
      continue

    try:
      process_recording(storage, species_in_area, recording_doc)
    except Exception as exc:
      print("Processing error: " + str(exc))
      storage.update_audio(
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


def main() -> None:
  RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
  RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
  ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)

  print(f"Initializing {CONFIG.get('storage_backend', 'sqlite')} storage...")
  storage = create_available_storage(CONFIG)
  print("Storage initialized!")

  print("Initializing birdnet...")
  configure_audio_input()

  print(
    "Using location: "
    f"{CONFIG['location_latitude']}, {CONFIG['location_longitude']}"
  )

  # Get species at location using geo model
  species_in_area = None
  if GEO_MODEL is not None:
    try:
      # Get current week of year (1-48)
      current_date = datetime.datetime.now(datetime.timezone.utc)
      week = (current_date.timetuple().tm_yday - 1) // 7 + 1
      
      # Predict species for location and time
      geo_predictions = GEO_MODEL.predict(  # type: ignore
        CONFIG["location_latitude"],
        CONFIG["location_longitude"],
        week=week
      )
      
      # Convert to dict of species names and their probabilities
      species_in_area = {
        str(species_name): float(probability)
        for species_name, probability in zip(geo_predictions.species_list, geo_predictions.species_probs)
      }
      
      print("Found " + str(len(species_in_area)) + " species in your area")
    except Exception as e:
      print(f"Warning: Could not get species by location ({e})")
      species_in_area = None
  else:
    print(
      "Geo model unavailable "
      f"({geo_model_error}); proceeding without geographical filtering"
    )
  
  write_status("running", {"species_count": len(species_in_area) if species_in_area else 0}, force=True)

  recorder = threading.Thread(target=recording_loop, args=(storage,), daemon=True)
  processor = threading.Thread(target=processor_loop, args=(storage, species_in_area), daemon=True)
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
    write_status("stopped", force=True)


if __name__ == "__main__":
  main()
