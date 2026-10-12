"""Register the second private inference node using its credentials over existing SSH."""

import json
import getpass
import http.cookiejar
import os
from pathlib import Path
import subprocess
import time
import urllib.request

root = Path(__file__).resolve().parents[1]
remote = subprocess.check_output(
    [
        "ssh",
        "-o",
        "BatchMode=yes",
        "dgx2-fast",
        "cat /home/sakana/work/image-gen-infra/.env",
    ],
    text=True,
)
node = dict(line.split("=", 1) for line in remote.splitlines() if "=" in line)
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)


def request(method, path, body=None):
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(
        "http://127.0.0.1:18080" + path,
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    with opener.open(req, timeout=15) as response:
        return json.load(response)


request(
    "POST",
    "/api/auth/login",
    {
        "username": os.environ.get("PLATFORM_USERNAME", "admin"),
        "password": os.environ.get("PLATFORM_PASSWORD", "").rstrip("\r\n")
        or getpass.getpass("Admin password: "),
    },
)
url = "http://" + node["INFERENCE_BIND_IP"] + ":" + node["INFERENCE_PORT"]
rows = request("GET", "/api/admin/backends")["nodes"]
existing = next((row for row in rows if row["base_url"] == url), None)
payload = {
    "name": "DGX 2 · spark-d705",
    "base_url": url,
    "inference_token": node["INFERENCE_TOKEN"],
    "admin_token": node["INFERENCE_ADMIN_TOKEN"],
    "enabled": True,
}
result = request(
    "PUT" if existing else "POST",
    "/api/admin/backends" + ("/" + existing["id"] if existing else ""),
    payload,
)
deadline = time.monotonic() + 45
while True:
    request("POST", "/api/admin/backends/sync", {})
    rows = request("GET", "/api/admin/backends")["nodes"]
    enabled = [row for row in rows if row["enabled"]]
    if len(enabled) == 2 and all(
        row["connected"] and len(row["models"]) == 2 and not row["catalog_errors"]
        for row in enabled
    ):
        break
    if time.monotonic() >= deadline:
        raise RuntimeError("Both backends must connect with valid catalogues")
    time.sleep(2)
catalogue = request("GET", "/api/models")
assert len(catalogue["models"]) == 2, "Expected two aggregated models"
report = {
    "passed": True,
    "second_node_id": result["id"],
    "nodes": [
        {
            "id": r["id"],
            "name": r["name"],
            "base_url": r["base_url"],
            "models": [
                {
                    "id": m["model_id"],
                    "revision": m["revision"],
                    "loras": len(m["loras"]),
                }
                for m in r["models"]
            ],
        }
        for r in enabled
    ],
    "aggregated_models": len(catalogue["models"]),
}
out = root / "verification-output/second-backend"
out.mkdir(parents=True, exist_ok=True)
(out / "connection-report.json").write_text(
    json.dumps(report, indent=2), encoding="utf-8"
)
print(json.dumps(report))
request("POST", "/api/auth/logout", {})
