"""One-time setup for the Elasticsearch storage backend.

Starts the Elasticsearch container defined in docker-compose.elastic.yml and
waits until it reports healthy. Requires Docker Desktop/Engine (with the
Compose plugin, or the standalone docker-compose) to be installed and running.

Usage:
  python scripts/setup_elasticsearch.py
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.elastic.yml"
CONTAINER_NAME = "bird-analyzer-elastic"
HEALTH_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 3


def run(cmd: list[str]) -> subprocess.CompletedProcess:
  return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)


def find_compose_command() -> list[str] | None:
  if shutil.which("docker"):
    probe = run(["docker", "compose", "version"])
    if probe.returncode == 0:
      return ["docker", "compose"]
  if shutil.which("docker-compose"):
    return ["docker-compose"]
  return None


def main() -> None:
  if not COMPOSE_FILE.exists():
    print(f"Could not find {COMPOSE_FILE}")
    sys.exit(1)

  if not shutil.which("docker"):
    print("Docker is required but was not found on PATH. Install Docker and try again.")
    sys.exit(1)

  compose_cmd = find_compose_command()
  if compose_cmd is None:
    print("Docker Compose (plugin or standalone) is required but was not found on PATH.")
    sys.exit(1)

  print("Starting Elasticsearch via docker compose...")
  result = run([*compose_cmd, "-f", str(COMPOSE_FILE), "up", "-d"])
  print(result.stdout.strip())
  if result.returncode != 0:
    print(result.stderr.strip())
    sys.exit(result.returncode)

  print("Waiting for Elasticsearch to report healthy...")
  deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
  healthy = False
  while time.monotonic() < deadline:
    status = run(["docker", "inspect", "--format", "{{.State.Health.Status}}", CONTAINER_NAME])
    if status.stdout.strip() == "healthy":
      healthy = True
      break
    time.sleep(POLL_INTERVAL_SECONDS)

  if not healthy:
    print(f"Timed out waiting for Elasticsearch to become healthy. Check `docker logs {CONTAINER_NAME}`.")
    sys.exit(1)

  print()
  print("Elasticsearch is up at http://localhost:9200")
  print("This compose file disables security/TLS for local development, so set the following in config.py:")
  print('  storage_backend = "elasticsearch"')
  print('  elasticsearch_host = "http://localhost:9200"')
  print('  elasticsearch_user = "elastic"')
  print('  elasticsearch_password = ""')


if __name__ == "__main__":
  main()
