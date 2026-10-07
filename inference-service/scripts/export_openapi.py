"""Write OpenAPI JSON (valid YAML 1.2) from the FastAPI application."""

import json
from pathlib import Path

from inference_service.app import create_app
from inference_service.config import Settings


project = Path(__file__).resolve().parents[2]
schema = create_app(Settings(project, "schema-inference-token", "schema-admin-token", "cpu", "float32")).openapi()
target = project / "contracts" / "inference.openapi.yaml"
target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(target)
