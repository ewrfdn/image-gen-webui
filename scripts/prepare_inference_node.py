"""Configure a standalone inference node with independent, persistent credentials."""

import argparse
import ipaddress
from pathlib import Path
import secrets

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--bind-ip", required=True)
parser.add_argument("--instance", default="dgx-2")
args = parser.parse_args()
address = ipaddress.ip_address(args.bind_ip)
if not address.is_private or address.is_loopback:
    raise ValueError("Use the private interconnect IP")
args.root.mkdir(parents=True, exist_ok=True)
env = args.root / ".env"
if env.exists():
    print("Existing inference credentials retained")
else:
    values = {
        "SERVICE_VERSION": "0.2.0",
        "INFERENCE_TOKEN": secrets.token_urlsafe(32),
        "INFERENCE_ADMIN_TOKEN": secrets.token_urlsafe(32),
        "INSTANCE_ID": args.instance,
        "INFERENCE_BIND_IP": args.bind_ip,
        "INFERENCE_PORT": "18001",
        "HOST_MODEL_ROOT": str((args.root / "models").resolve()),
    }
    env.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    env.chmod(0o600)
    print("Standalone inference configuration written; credentials hidden")
