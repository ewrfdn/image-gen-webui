import io
import json
from pathlib import Path

import pytest
from PIL import Image

from inference_service.app import create_app
from inference_service.config import Settings


class FakeBackend:
    def __init__(self):
        self.loads = 0
        self.calls = []
        self.wait_started = None
        self.wait_release = None
        self.fail_cleanup = False

    def load(self, checkpoint):
        self.loads += 1

    def unload(self):
        pass

    def generate(self, request, lora, seed):
        self.calls.append((lora.lora_id if lora else None, request.loras[0].scale if lora else None, seed))
        if self.wait_started:
            self.wait_started.set()
            assert self.wait_release.wait(5)
        if self.fail_cleanup:
            from inference_service.backends import CleanupFailed
            raise CleanupFailed()
        output = io.BytesIO()
        Image.new("RGB", (1, 1), "red").save(output, format="PNG")
        return output.getvalue()


@pytest.fixture
def project(tmp_path: Path):
    model = tmp_path / "sdxl-base" / "model"
    model.mkdir(parents=True)
    (model / "model_index.json").write_text(json.dumps({"_class_name": "StableDiffusionXLPipeline"}))
    for component in ("unet", "vae", "text_encoder", "text_encoder_2", "tokenizer", "tokenizer_2", "scheduler"):
        part = model / component
        part.mkdir()
        (part / ("weights.safetensors" if component in ("unet", "vae", "text_encoder", "text_encoder_2") else "config.json")).write_text("x")
    lora_dir = model.parent / "loras"
    lora_dir.mkdir()
    header = json.dumps({"tensor": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    (lora_dir / "style.safetensors").write_bytes(len(header).to_bytes(8, "little") + header + b"\0\0\0\0")
    backend = FakeBackend()
    settings = Settings(tmp_path, "infer-secret", "admin-secret", "cpu", "float32", "test-node")
    return create_app(settings, lambda: backend), backend, tmp_path


@pytest.fixture
def admin_headers():
    return {"Authorization": "Bearer admin-secret"}


@pytest.fixture
def infer_headers():
    return {"Authorization": "Bearer infer-secret"}
