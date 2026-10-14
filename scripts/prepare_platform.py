"""Prepare local secrets and isolated resource mounts. Never print credentials."""

import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path.home() / "models")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [
            sys.executable,
            str(root / "inference-service/scripts/prepare_dgx_dev.py"),
            "--source",
            str(args.source),
        ],
        check=True,
    )
    infra = {}
    for line in (root / "inference-service/.env").read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            infra[k] = v
    env = root / ".env"
    if not env.exists():
        values = {
            "SERVICE_VERSION": "0.2.0",
            "PLATFORM_PORT": "18080",
            "BIND_ADDRESS": "127.0.0.1",
            "DATABASE_PASSWORD": secrets.token_hex(24),
            "PLATFORM_ADMIN_TOKEN": secrets.token_urlsafe(32),
            "PLATFORM_ENCRYPTION_KEY": base64.urlsafe_b64encode(
                secrets.token_bytes(32)
            ).decode(),
            "INFERENCE_TOKEN": infra["INFERENCE_TOKEN"],
            "INFERENCE_ADMIN_TOKEN": infra["ADMIN_TOKEN"],
            "HOST_MODEL_ROOT": infra["HOST_MODEL_ROOT"],
        }
        env.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        env.chmod(0o600)
    override = json.loads(
        (root / "inference-service/compose.resources.json").read_text()
    )
    (root / "compose.resources.json").write_text(json.dumps(override, indent=2))
    data = root / "platform-data"
    data.mkdir(exist_ok=True)
    if os.name != "nt":
        # Compose server runs UID 1000, matching the DGX operator in this deployment.
        if data.stat().st_uid != 1000:
            print("Ensure platform-data is writable by container UID 1000")
    print(
        "Platform environment and resource override prepared; credentials preserved and hidden"
    )


if __name__ == "__main__":
    main()
