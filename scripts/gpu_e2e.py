"""Real platform GPU acceptance on DGX; credentials never enter reports.

Run from the deployment directory. Stops only other currently running containers,
records IDs before interruption, and restores them in finally. Never starts a
container that was stopped before this test. UI verification may signal ui.done.
"""

import argparse
import base64
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse completed checks from this deployment after test-tool failure",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = root / "verification-output/gpu-e2e"
    out.mkdir(parents=True, exist_ok=True)
    (out / "ui.done").unlink(missing_ok=True)
    env = dict(
        line.split("=", 1)
        for line in (root / ".env").read_text().splitlines()
        if "=" in line
    )
    admin = env["PLATFORM_ADMIN_TOKEN"]
    report = {"passed": False, "phase": "inventory", "checks": [], "tasks": []}
    if args.resume:
        report = json.loads((out / "report.json").read_text())
        if report["phase"] != "finished" or not all(
            r["running"] for r in report["restored"]
        ):
            raise RuntimeError(
                "Previous run must have finished restoring containers before resume"
            )
        report.setdefault("tool_failures", []).append(report.pop("error", "unknown"))
        report["passed"] = False
    key = None
    paused = []

    def save():
        (out / "report.json").write_text(json.dumps(report, indent=2))

    def check(name, condition, **details):
        report["checks"].append({"name": name, "passed": bool(condition), **details})
        save()
        print(name, "PASS" if condition else "FAIL", flush=True)
        if not condition:
            raise RuntimeError(name)

    def docker(*args, timeout=180):
        return subprocess.check_output(
            ["docker", *args], text=True, timeout=timeout
        ).strip()

    def request(method, path, body=None, token=None, raw=False, headers=None):
        h = {
            "Authorization": "Bearer " + (token or admin),
            "Content-Type": "application/json",
            **(headers or {}),
        }
        req = urllib.request.Request(
            "http://127.0.0.1:18080" + path,
            headers=h,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=1800) as response:
                data = response.read()
                return (
                    response.status,
                    data if raw else json.loads(data),
                    dict(response.headers),
                )
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), dict(exc.headers)

    def unload(model):
        code = (
            'import os,httpx; r=httpx.post("http://127.0.0.1:8000/internal/v1/models/'
            + model
            + '/unload",headers={"Authorization":"Bearer "+os.environ["ADMIN_TOKEN"]},json={},timeout=180);r.raise_for_status();print(r.json()["state"])'
        )
        check(
            "unload_" + model,
            docker("exec", "image-gen-platform-inference-1", "python", "-c", code)
            == "unloaded",
        )
        request("POST", "/api/admin/backends/sync", {})

    def wait_task(task_id, token):
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            code, task, _ = request("GET", "/api/tasks/" + task_id, token=token)
            if task.get("status") in ("succeeded", "failed", "unknown"):
                check(
                    "task_succeeded_" + task_id,
                    code == 200 and task["status"] == "succeeded",
                    task=task,
                )
                return task
            time.sleep(2)
        raise TimeoutError("GPU task did not finish: " + task_id)

    def generate(model, name, lora=None, sync=False):
        body = {
            "model": model["model_id"],
            "revision": model["revision"],
            "prompt": "a small cabin in a snowy forest, detailed illustration",
            "size": "512x512",
            "seed": 1234,
            "num_inference_steps": 4,
        }
        if lora:
            body["loras"] = [{"lora_id": lora, "scale": 0.8}]
        start = time.monotonic()
        code, result, headers = request(
            "POST",
            "/v1/images/generations",
            body,
            token=key["key"],
            headers={} if sync else {"Prefer": "respond-async"},
        )
        check(name + "_accepted", code == (200 if sync else 202), status=code)
        task_id = next(
            (value for name, value in headers.items() if name.lower() == "x-task-id"),
            None,
        ) or result.get("id")
        task = wait_task(task_id, key["key"])
        code, png, _ = request(
            "GET", task["files"][0]["url"], token=key["key"], raw=True
        )
        check(
            name + "_persisted",
            code == 200
            and task["node_id"]
            and task["model_revision"] == model["revision"]
            and task["files"][0]["storage_key"],
        )
        (out / (name + ".png")).write_bytes(png)
        pinned = json.loads(
            docker(
                "exec",
                "image-gen-platform-database-1",
                "psql",
                "-U",
                "image_gen",
                "-d",
                "image_gen",
                "-Atc",
                "SELECT json_build_object('sha256',model_sha256,'loras',loras) FROM media_tasks WHERE id='"
                + task_id
                + "'",
            )
        )
        check(
            name + "_database_versions",
            pinned["sha256"] == model["artifact_sha256"]
            and (
                not lora
                or any(
                    item["lora_id"] == lora
                    and item["sha256"]
                    == next(w["sha256"] for w in model["loras"] if w["lora_id"] == lora)
                    for item in pinned["loras"]
                )
            ),
        )
        # Decode using Pillow in the deployed server image, including entropy check.
        validation = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "image-gen-platform-server-1",
                "python",
                "-c",
                'import sys,io,json,hashlib;from PIL import Image,ImageStat;im=Image.open(io.BytesIO(sys.stdin.buffer.read())).convert("RGB");im.load();print(json.dumps({"size":im.size,"stddev":max(ImageStat.Stat(im).stddev),"pixels":hashlib.sha256(im.tobytes()).hexdigest()}))',
            ],
            input=png,
            capture_output=True,
            timeout=30,
            check=True,
        )
        info = json.loads(validation.stdout)
        check(
            name + "_decoded", info["size"] == [512, 512] and info["stddev"] > 1, **info
        )
        if sync:
            check(
                name + "_sync_b64_matches_download",
                base64.b64decode(result["data"][0]["b64_json"], validate=True) == png,
            )
        report["tasks"].append(
            {
                "id": task_id,
                "model": model["model_id"],
                "name": name,
                "seconds": round(time.monotonic() - start, 2),
            }
        )
        save()
        return info["pixels"]

    try:
        running = docker("ps", "-q").splitlines()
        inventory = json.loads(docker("inspect", *running))
        # Persist minimal recovery journal; docker inspect environment may contain secrets.
        report["before"] = [
            {"id": c["Id"], "name": c["Name"], "status": c["State"]["Status"]}
            for c in inventory
        ]
        save()
        for c in inventory:
            if c["Name"].startswith("/image-gen-platform-"):
                continue
            paused.append(c["Id"])
            report["paused"] = paused
            save()
            docker("stop", "--time", "30", c["Id"])
        check("platform_ready", request("GET", "/health/ready")[0] == 200)
        key = request("POST", "/api/admin/keys", {"name": "real-gpu-e2e"})[1]
        models = {m["model_id"]: m for m in request("GET", "/api/models")[1]["models"]}
        report["phase"] = "qwen_gpu"
        save()
        qwen = models["qwen-image-2.1"]
        if not (
            args.resume
            and any(
                c["name"] == "unload_qwen-image-2.1" and c["passed"]
                for c in report["checks"]
            )
        ):
            baseline = generate(qwen, "qwen_baseline")
            lora_id = qwen["loras"][0]["lora_id"]
            adapted = generate(qwen, "qwen_lora", lora_id)
            check("lora_changes_pixels", adapted != baseline)
            check("lora_isolation", generate(qwen, "qwen_after_lora") == baseline)
            unload(qwen["model_id"])
        report["phase"] = "sdxl_gpu"
        save()
        generate(models["noobai-XL-1.1"], "sdxl_sync", sync=True)
        code, _, _ = request(
            "GET", "/api/tasks/" + report["tasks"][0]["id"], token="invalid"
        )
        check("task_auth_required", code == 401)
        report["phase"] = "ui_ready"
        save()
        print("UI_READY", flush=True)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline and not (out / "ui.done").exists():
            time.sleep(2)
        check("live_ui_gpu", (out / "ui.done").exists())
        unload("noobai-XL-1.1")
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        report["phase"] = "restoring"
        save()
        if key:
            try:
                request("PATCH", "/api/admin/keys/" + key["id"], {"revoked": True})
            except Exception:  # noqa: BLE001 -- key cleanup must never prevent container restoration
                report["key_cleanup_failed"] = True
        restoration = []
        for container_id in paused:
            try:
                docker("start", container_id)
                restoration.append(
                    {
                        "id": container_id,
                        "running": docker(
                            "inspect", "--format", "{{.State.Running}}", container_id
                        )
                        == "true",
                    }
                )
            except Exception as exc:  # noqa: BLE001 -- continue restoring remaining containers on any individual failure
                restoration.append(
                    {"id": container_id, "running": False, "error": str(exc)}
                )
        report["restored"] = restoration
        report["passed"] = report["passed"] and all(c["running"] for c in restoration)
        report["phase"] = "finished"
        save()


if __name__ == "__main__":
    main()
