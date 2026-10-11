"""Export schemas without starting a database or loading a GPU model."""

import argparse
import json
from pathlib import Path
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("service", choices=["inference", "server"])
args = parser.parse_args()
sys.path.insert(
    0, str(root / ("inference-service" if args.service == "inference" else "server"))
)
with tempfile.TemporaryDirectory() as tmp:
    if args.service == "inference":
        from inference_service.app import create_app
        from inference_service.config import Settings

        app = create_app(
            Settings(
                Path(tmp),
                "contract-inference-placeholder",
                "contract-admin-placeholder",
                device="cpu",
            )
        )
    else:
        from cryptography.fernet import Fernet
        from platform_service.app import create_app
        from platform_service.config import Config

        app = create_app(
            Config(
                "sqlite+aiosqlite:///:memory:",
                "contract-admin-placeholder-00000",
                Fernet.generate_key().decode(),
                Path(tmp),
                False,
            )
        )
    path = root / "contracts" / f"{args.service}.openapi.yaml"
    path.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(path.name, "exported")
