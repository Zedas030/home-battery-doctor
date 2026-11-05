"""Docker HEALTHCHECK: asks the /health endpoint of the service."""
import os
import sys
import urllib.request

import yaml

try:
    with open(os.environ.get("AKD_CONFIG", "/config/config.yaml"), encoding="utf-8") as f:
        port = int(((yaml.safe_load(f) or {}).get("web") or {}).get("port", 8104))
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=4) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:  # noqa: BLE001
    sys.exit(1)
