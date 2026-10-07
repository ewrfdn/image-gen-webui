"""HTTP smoke test for a deployed inference service. Does not modify model files."""

import argparse
import base64
import json
import os
import urllib.error
import urllib.request


def call(base: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{path}: HTTP {exc.code}: {exc.read().decode()[:500]}") from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--lora-id")
    parser.add_argument("--size", default="512x512")
    parser.add_argument("--prompt", default="a small cabin in a snowy forest")
    args = parser.parse_args()
    base = os.environ.get("INFERENCE_BASE_URL", "http://127.0.0.1:8000")
    admin = os.environ["ADMIN_TOKEN"]
    infer = os.environ["INFERENCE_TOKEN"]
    print("health:", call(base, "/health/ready"))
    print("load:", call(base, f"/internal/v1/models/{args.model}/load", admin, {}))
    payload = {"model": args.model, "prompt": args.prompt, "size": args.size,
               "n": 1, "response_format": "b64_json", "num_inference_steps": 4}
    if args.lora_id:
        payload["loras"] = [{"lora_id": args.lora_id, "scale": 0.8}]
    result = call(base, "/v1/images/generations", infer, payload)
    png = base64.b64decode(result["data"][0]["b64_json"], validate=True)
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Generation did not return PNG")
    print("png_bytes:", len(png))
    print("metadata:", json.dumps(result["metadata"], ensure_ascii=False))


if __name__ == "__main__":
    main()
