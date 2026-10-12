"""Local UI verification against a real server process and a controlled in-memory backend."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile
import httpx
import uvicorn
from cryptography.fernet import Fernet
from platform_service.app import create_app
from platform_service.config import Config
from test_platform import Backend, model

backend = Backend()
backend.models = [model(references=True), model("video", True)]
backend.models[1]["loras"][0]["lora_id"] = "video-style"
backend.models[1]["loras"][0]["display_name"] = "Video Style"
folder = Path(tempfile.mkdtemp(prefix="frame-ui-check-"))
config = Config(
    "sqlite+aiosqlite:///" + str(folder / "db.sqlite"),
    "ui-verification-token-0000000000",
    Fernet.generate_key().decode(),
    folder / "data",
    True,
)
app = create_app(
    config, httpx.AsyncClient(transport=httpx.MockTransport(backend.handle))
)
uvicorn.run(app, host="127.0.0.1", port=18082)
