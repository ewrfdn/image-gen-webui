"""Real GPU acceptance: PNG decoding, LoRA isolation, busy behavior and lifecycle.

Run inside the deployed container with its two tokens in the environment.
Artifacts contain images and measurements, never credentials.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import time

import httpx
from PIL import Image, ImageStat


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--lora-id")
    parser.add_argument("--steps", type=int, default=4)
    parser.add_argument("--size", default="512x512")
    parser.add_argument("--prompt", default="a small cabin in a snowy forest, detailed illustration")
    parser.add_argument("--output", default="/tmp/verification-output")
    parser.add_argument("--keep-loaded", action="store_true", help="Skip the extra unload/reload cycle")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report = {"model": args.model, "size": args.size, "steps": args.steps, "passed": False, "checks": []}
    client = httpx.Client(base_url=os.environ.get("INFERENCE_BASE_URL", "http://127.0.0.1:8000"), timeout=1800)
    admin = {"Authorization": "Bearer " + os.environ["ADMIN_TOKEN"]}
    infer = {"Authorization": "Bearer " + os.environ["INFERENCE_TOKEN"]}

    def check(name, condition, **details):
        report["checks"].append({"name": name, "passed": bool(condition), **details})
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(name, "PASS" if condition else "FAIL", details, flush=True)
        if not condition:
            raise RuntimeError(name)

    def post(path, headers, body=None):
        response = client.post(path, headers=headers, json=body or {})
        response.raise_for_status()
        return response.json()

    path = f"/internal/v1/models/{args.model}"
    check("health", client.get("/health/ready").status_code == 200)
    check("admin_auth", client.get("/internal/v1/models").status_code == 401)
    check("token_separation", client.get("/internal/v1/models", headers=infer).status_code == 401)
    report["resources_before"] = client.get("/internal/v1/resources", headers=admin).json()
    started = time.monotonic()
    loaded = post(path + "/load", admin)
    check("load", loaded["state"] == "ready", seconds=round(time.monotonic() - started, 2))
    payload = {"model": args.model, "prompt": args.prompt,
               "size": args.size, "seed": 1234, "num_inference_steps": args.steps}

    def generate(name, lora=None):
        body = dict(payload)
        if lora:
            body["loras"] = [{"lora_id": lora, "scale": 0.8}]
        result = post("/v1/images/generations", infer, body)
        check(name + "_lora_metadata", bool(result["metadata"]["loras"]) == bool(lora) and
              (not lora or result["metadata"]["loras"][0]["lora_id"] == lora))
        png = base64.b64decode(result["data"][0]["b64_json"], validate=True)
        image = Image.open(io.BytesIO(png)).convert("RGB")
        image.load()
        digest = hashlib.sha256(image.tobytes()).hexdigest()
        (output / (name + ".png")).write_bytes(png)
        check(name, image.size == tuple(map(int, args.size.split("x"))) and max(ImageStat.Stat(image).stddev) > 1,
              pixel_sha256=digest, png_bytes=len(png), metadata=result["metadata"])
        return digest

    baseline = generate("baseline")
    if args.lora_id:
        adapted = generate("with_lora", args.lora_id)
        check("lora_changes_pixels", adapted != baseline)
        after = generate("after_lora")
        check("lora_isolation_exact_pixels", baseline == after)
    invalid = dict(payload, loras=[{"lora_id": "missing-acceptance-test", "scale": 1}])
    response = client.post("/v1/images/generations", headers=infer, json=invalid)
    check("invalid_lora", response.status_code == 404 and response.json()["error"]["code"] == "lora_not_found")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(generate, "concurrency_primary")
        busy = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not pending.done():
            caps = client.get("/internal/v1/capabilities", headers=admin).json()
            if caps["busy"]:
                busy = True
                break
            time.sleep(0.1)
        if busy:
            response = client.post("/v1/images/generations", headers=infer, json=payload)
            check("concurrent_rejected", response.status_code == 503 and
                  response.json()["error"]["code"] == "engine_busy" and response.headers.get("retry-after") == "1")
        pending.result()
        check("busy_observed", busy)
    report["resources_loaded"] = client.get("/internal/v1/resources", headers=admin).json()
    report["lifecycle_tested"] = not args.keep_loaded
    if not args.keep_loaded:
        check("unload", post(path + "/unload", admin)["state"] == "unloaded")
        response = client.post("/v1/images/generations", headers=infer, json=payload)
        check("unloaded_rejected", response.status_code == 409 and response.json()["error"]["code"] == "model_not_loaded")
        check("reload", post(path + "/load", admin)["state"] == "ready")
        generate("after_reload")
    report["resources_after"] = client.get("/internal/v1/resources", headers=admin).json()
    report["passed"] = True
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Acceptance completed; model remains ready", flush=True)


if __name__ == "__main__":
    main()
