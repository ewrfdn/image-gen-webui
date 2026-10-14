"""Live deployment smoke checks. Does not submit any GPU generation request."""

import argparse
import json
from pathlib import Path
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    env = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (root / ".env").read_text().splitlines()
        if "=" in line
    }
    checks = []

    def request(method, path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            args.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def check(name, condition, **details):
        checks.append({"name": name, "passed": bool(condition), **details})
        if not condition:
            raise RuntimeError("Failed " + name)

    admin = env["PLATFORM_ADMIN_TOKEN"]
    check("health_ready", request("GET", "/health/ready")[0] == 200)
    check("auth_required", request("GET", "/api/models")[0] == 401)
    code, nodes = request("GET", "/api/admin/backends", token=admin)
    check("backend_admin", code == 200)
    if not any(n["base_url"] == "http://inference:8000" for n in nodes["nodes"]):
        code, node = request(
            "POST",
            "/api/admin/backends",
            {
                "name": "local-dgx",
                "base_url": "http://inference:8000",
                "inference_token": env["INFERENCE_TOKEN"],
                "admin_token": env["INFERENCE_ADMIN_TOKEN"],
            },
            admin,
        )
        check("backend_registered", code == 200)
    deadline = time.monotonic() + 30
    while True:
        code, catalog = request("GET", "/api/models", token=admin)
        if catalog.get("models") or time.monotonic() >= deadline:
            break
        time.sleep(2)
    check(
        "real_model_catalogue",
        code == 200 and len(catalog["models"]) >= 2,
        models=[
            {
                "id": m["model_id"],
                "loras": len(m["loras"]),
                "reference": m["configuration"]["reference"]["supported"],
            }
            for m in catalog["models"]
        ],
    )
    key = None
    try:
        code, key = request(
            "POST", "/api/admin/keys", {"name": "deployment-verification"}, admin
        )
        check("api_key_created", code == 200)
        check(
            "key_model_access", request("GET", "/v1/models", token=key["key"])[0] == 200
        )
        check(
            "key_cannot_admin",
            request("GET", "/api/admin/settings", token=key["key"])[0] == 403,
        )
        check(
            "key_not_revealed_in_listing",
            "key" not in request("GET", "/api/admin/keys", token=admin)[1]["keys"][0],
        )
        check(
            "api_key_revoked",
            request("PATCH", "/api/admin/keys/" + key["id"], {"revoked": True}, admin)[
                0
            ]
            == 200,
        )
        check(
            "revoked_key_rejected",
            request("GET", "/v1/models", token=key["key"])[0] == 401,
        )
        key = None
        code, setting = request("GET", "/api/admin/settings", token=admin)
        check(
            "fixed_time_limits",
            setting["query_timeout_seconds"] == 1
            and setting["idle_release_seconds"] == 3600,
        )
        first = catalog["models"][0]
        start = time.monotonic()
        code, availability = request(
            "GET",
            "/api/cluster/availability?model_id=" + first["model_id"],
            token=admin,
        )
        elapsed = time.monotonic() - start
        check(
            "one_second_availability",
            code == 200 and elapsed < 1.2,
            elapsed_seconds=round(elapsed, 3),
            status=availability["status"],
        )
        check(
            "ui_served", urllib.request.urlopen(args.base_url, timeout=5).status == 200
        )
    finally:
        if key:
            request("PATCH", "/api/admin/keys/" + key["id"], {"revoked": True}, admin)
        directory = root / "verification-output"
        directory.mkdir(exist_ok=True)
        report = {
            "passed": all(c["passed"] for c in checks),
            "gpu_generation_tested": False,
            "checks": checks,
        }
        (directory / "platform-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
