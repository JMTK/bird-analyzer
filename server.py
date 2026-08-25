import datetime
import json
import importlib.util
from pathlib import Path

from flask import Flask, jsonify, render_template

from storage import create_storage

STATUS_PATH = Path("runtime") / "status.json"
HEARTBEAT_TIMEOUT_SECONDS = 12

app = Flask(__name__)


def load_runtime_config() -> dict:
    candidate_files = [Path("config.py"), Path("config.example.py")]
    for config_path in candidate_files:
        if config_path.exists():
            spec = importlib.util.spec_from_file_location("bird_config", config_path)
            module = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(module)
            return {
                "elasticsearch_host": getattr(module, "elasticsearch_host", "https://localhost:9200"),
                "elasticsearch_user": getattr(module, "elasticsearch_user", "elastic"),
                "elasticsearch_password": getattr(module, "elasticsearch_password", ""),
                "cert_loc": getattr(module, "cert_loc", str(Path.cwd() / "http_ca.crt")),
                "storage_backend": getattr(module, "storage_backend", "elasticsearch"),
                "sqlite_path": getattr(module, "sqlite_path", str(Path.cwd() / "runtime" / "bird-analyzer.db")),
            }
    raise FileNotFoundError("Missing config.py or config.example.py")


CONFIG = load_runtime_config()
storage = create_storage(CONFIG)


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


@app.get("/")
def dashboard() -> str:
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    return jsonify(read_runtime_status())


@app.get("/api/audio")
def api_audio():
    docs = fetch_docs("audio")
    return jsonify({"count": len(docs), "items": docs})


@app.get("/api/processed")
def api_processed():
    docs = fetch_docs("metadata")
    return jsonify({"count": len(docs), "items": docs})


@app.get("/api/space")
def api_space():
    audio_docs = fetch_docs("audio", size=400)
    points = []
    for doc in audio_docs:
        if doc.get("status") != "processed":
            continue
        points.append(
            {
                "recording_id": doc.get("recording_id") or doc.get("_id"),
                "timestamp": doc.get("timestamp"),
                "species": doc.get("top_prediction") or "Unknown",
                "x_pitch_hz": doc.get("pitch_hz", 0.0),
                "y_timbre_centroid_hz": doc.get("spectral_centroid_hz", 0.0),
                "z_tonal_spread_hz": doc.get("spectral_bandwidth_hz", 0.0),
                "energy_rms": doc.get("rms", 0.0),
                "confidence": doc.get("top_confidence", 0.0),
                "file_path": doc.get("file_path", ""),
            }
        )
    return jsonify({"count": len(points), "items": points})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
