import datetime
import json
import importlib.util
from pathlib import Path

import elasticsearch
from flask import Flask, jsonify, render_template

AUDIO_INDEX_NAME = "bird-audio"
METADATA_INDEX_NAME = "bird-analyzer"
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
            }
    raise FileNotFoundError("Missing config.py or config.example.py")


CONFIG = load_runtime_config()


def create_es_client() -> elasticsearch.Elasticsearch:
    return elasticsearch.Elasticsearch(
        CONFIG["elasticsearch_host"],
        ca_certs=CONFIG["cert_loc"],
        http_auth=(CONFIG["elasticsearch_user"], CONFIG["elasticsearch_password"]),
        max_retries=0,
        retry_on_timeout=False,
    )


es = create_es_client()


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
    try:
        response = es.search(
            index=index_name,
            size=size,
            sort=[{"timestamp": {"order": "desc", "unmapped_type": "date"}}],
            query={"match_all": {}},
        )
        hits = response.get("hits", {}).get("hits", [])
        docs = []
        for hit in hits:
            item = hit.get("_source", {})
            item["_id"] = hit.get("_id")
            docs.append(item)
        return docs
    except Exception:
        return []


@app.get("/")
def dashboard() -> str:
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    return jsonify(read_runtime_status())


@app.get("/api/audio")
def api_audio():
    docs = fetch_docs(AUDIO_INDEX_NAME)
    return jsonify({"count": len(docs), "items": docs})


@app.get("/api/processed")
def api_processed():
    docs = fetch_docs(METADATA_INDEX_NAME)
    return jsonify({"count": len(docs), "items": docs})


@app.get("/api/space")
def api_space():
    audio_docs = fetch_docs(AUDIO_INDEX_NAME, size=400)
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
