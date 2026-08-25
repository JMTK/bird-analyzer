import datetime
import json
import importlib.util
from pathlib import Path

from flask import Flask, jsonify, render_template
import requests

from bird import Response
from storage import create_available_storage

STATUS_PATH = Path("runtime") / "status.json"
HEARTBEAT_TIMEOUT_SECONDS = 12

app = Flask(__name__)


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
                "enable_online_enrichment": getattr(module, "enable_online_enrichment", False),
                "nuthatch_hourly_limit": getattr(module, "nuthatch_hourly_limit", 500),
            }
    raise FileNotFoundError("Missing config.py or config.example.py")


CONFIG = load_runtime_config()
storage = create_available_storage(CONFIG)


def parse_timestamp(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return None


def read_runtime_status() -> dict:
    if not STATUS_PATH.exists():
        return {
            "analyzerRunning": False,
            "state": "missing",
            "reason": "No runtime status file found",
        }

    payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    heartbeat = parse_timestamp(payload.get("heartbeat"))
    now = datetime.datetime.now(datetime.timezone.utc)

    running = False
    if heartbeat:
        delta = (now - heartbeat).total_seconds()
        running = payload.get("state") == "running" and delta <= HEARTBEAT_TIMEOUT_SECONDS

    payload["analyzerRunning"] = running
    payload["heartbeatTimeoutSeconds"] = HEARTBEAT_TIMEOUT_SECONDS
    return payload


def fetch_docs(index_name: str, size: int = 250) -> list[dict]:
    if index_name == "audio":
        return storage.fetch_audio(size)
    return storage.fetch_metadata(size)


def metadata_by_recording(size: int = 1000) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for metadata in storage.fetch_metadata(size):
        recording_id = metadata.get("recording_id")
        if not recording_id:
            continue
        existing = results.get(recording_id)
        if existing is None or metadata.get("confidence", 0) > existing.get("confidence", 0):
            results[recording_id] = metadata
    return results


def display_species(audio_doc: dict, metadata: dict | None) -> str:
    return audio_doc.get("top_prediction") or (metadata or {}).get("name") or "Unknown"


def needs_enrichment(metadata: dict) -> bool:
    return not any(metadata.get(field) for field in ("family", "order", "status", "images"))


def enrich_metadata(metadata: dict) -> dict | None:
    if not CONFIG["enable_online_enrichment"] or not CONFIG["api_key"]:
        return None

    name = metadata.get("name", "")
    scientific_name = metadata.get("sciName", "")
    if not name and not scientific_name:
        return None

    cache_key = f"{scientific_name}|{name}"
    cached = storage.get_enrichment_cache(cache_key)
    if cached is not None:
        return cached
    if not storage.reserve_enrichment_request(CONFIG["nuthatch_hourly_limit"]):
        return None

    try:
        response = requests.get(
            "https://nuthatch.lastelm.software/v2/birds?page=1&pageSize=1"
            f"&name={name}&sciName={scientific_name}&operator=OR",
            headers={"API-Key": CONFIG["api_key"]},
            timeout=3,
        )
        response.raise_for_status()
        result = Response.from_dict(response.json())
    except (requests.RequestException, ValueError, TypeError):
        return None

    if not result.entities:
        return None
    bird = result.entities[0]
    enrichment = {
        "name": bird.name,
        "sciName": bird.sciName,
        "images": bird.images,
        "region": bird.region,
        "family": bird.family,
        "order": bird.order,
        "status": bird.status,
    }
    storage.set_enrichment_cache(cache_key, enrichment)
    return enrichment


@app.get("/")
def dashboard() -> str:
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    return jsonify(read_runtime_status())


@app.get("/api/audio")
def api_audio():
    docs = fetch_docs("audio")
    metadata_lookup = metadata_by_recording()
    for doc in docs:
        metadata = metadata_lookup.get(str(doc.get("recording_id") or ""))
        doc["display_species"] = display_species(doc, metadata)
        if not doc.get("top_confidence") and metadata:
            doc["display_confidence"] = metadata.get("confidence", 0.0)
    return jsonify({"count": len(docs), "items": docs})


@app.get("/api/processed")
def api_processed():
    docs = fetch_docs("metadata")
    return jsonify({"count": len(docs), "items": docs})


@app.post("/api/backfill")
def api_backfill():
    if not CONFIG["enable_online_enrichment"] or not CONFIG["api_key"]:
        return jsonify({"updated": 0, "reason": "Online enrichment is disabled or has no API key"}), 400

    updated = 0
    for metadata in storage.fetch_metadata(1000):
        if not needs_enrichment(metadata):
            continue
        enrichment = enrich_metadata(metadata)
        if enrichment is None:
            continue
        storage.update_metadata(str(metadata.get("_id")), enrichment)
        updated += 1
    return jsonify({"updated": updated})


@app.get("/api/space")
def api_space():
    audio_docs = fetch_docs("audio", size=400)
    metadata_lookup = metadata_by_recording()
    points = []
    for doc in audio_docs:
        if doc.get("status") != "processed":
            continue
        metadata = metadata_lookup.get(str(doc.get("recording_id") or ""))
        points.append(
            {
                "recording_id": doc.get("recording_id") or doc.get("_id"),
                "timestamp": doc.get("timestamp"),
                "species": display_species(doc, metadata),
                "x_pitch_hz": doc.get("pitch_hz", 0.0),
                "y_timbre_centroid_hz": doc.get("spectral_centroid_hz", 0.0),
                "z_tonal_spread_hz": doc.get("spectral_bandwidth_hz", 0.0),
                "energy_rms": doc.get("rms", 0.0),
                "confidence": doc.get("top_confidence") or (metadata or {}).get("confidence", 0.0),
                "file_path": doc.get("file_path", ""),
            }
        )
    return jsonify({"count": len(points), "items": points})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
