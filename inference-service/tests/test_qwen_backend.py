"""Qwen-Image 2.1 discovery and pipeline behavior without GPU dependencies."""

from contextlib import nullcontext
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from inference_service.backends.base import CleanupFailed, UnsupportedParameter
from inference_service.backends.factory import create_backend
from inference_service.backends.qwen_image_21.backend import QwenImage21Backend
from inference_service.config import Settings
from inference_service.engine.registry import Registry
from inference_service.resources import Lora
from inference_service.schemas import GenerationRequest


def add_qwen_checkpoint(root: Path) -> Path:
    model = root / "qwen-image-21" / "model"
    model.mkdir(parents=True)
    (model / "model_index.json").write_text(json.dumps({"_class_name": "QwenImage21Pipeline"}))
    for component in ("processor", "scheduler", "text_encoder", "transformer", "vae"):
        part = model / component
        part.mkdir()
        (part / "config.json").write_text("{}")
        if component in ("text_encoder", "transformer", "vae"):
            (part / "model.safetensors").write_bytes(b"weights")
    return model


def test_qwen_discovery_capabilities_and_size_validation(project, admin_headers, infer_headers):
    app, _, root = project
    add_qwen_checkpoint(root)
    checkpoint = Registry(root).checkpoints()["qwen-image-21"]
    assert checkpoint.architecture == "qwen_image_21"
    settings = Settings(root, "infer-secret", "admin-secret", "cpu", "float32", "test-node")
    assert isinstance(create_backend(checkpoint, settings), QwenImage21Backend)

    with TestClient(app) as client:
        models = client.get("/internal/v1/capabilities", headers=admin_headers).json()["models"]
        qwen = next(item for item in models if item["model_id"] == "qwen-image-21")
        assert qwen["architecture"] == "qwen_image_21"
        assert qwen["steps"]["default"] == 40
        assert qwen["guidance_scale"]["default"] == 1.0
        assert client.post("/internal/v1/models/qwen-image-21/load", headers=admin_headers).status_code == 200
        oversized = client.post("/v1/images/generations", headers=infer_headers,
                                json={"model": "qwen-image-21", "prompt": "test", "size": "2752x2752"})
        assert oversized.status_code == 400
        assert oversized.json()["error"]["code"] == "invalid_request"
        supported = client.post("/v1/images/generations", headers=infer_headers,
                                json={"model": "qwen-image-21", "prompt": "test", "size": "2048x2048"})
        assert supported.status_code == 200
        assert client.post("/internal/v1/models/qwen-image-21/unload", headers=admin_headers).status_code == 200
        assert client.post("/internal/v1/models/sdxl-base/load", headers=admin_headers).status_code == 200
        sdxl_oversized = client.post("/v1/images/generations", headers=infer_headers,
                                    json={"model": "sdxl-base", "prompt": "test", "size": "2048x2048"})
        assert sdxl_oversized.status_code == 400
        assert sdxl_oversized.json()["error"]["code"] == "unsupported_size"


def test_qwen_registry_rejects_incomplete_and_legacy_pipeline(project):
    _, _, root = project
    model = add_qwen_checkpoint(root)
    (model / "transformer" / "model.safetensors").unlink()
    registry = Registry(root)
    assert "qwen-image-21" not in registry.checkpoints()
    assert registry.checkpoints(include_invalid=True)["qwen-image-21"].reason == "missing_weights"
    (model / "model_index.json").write_text(json.dumps({"_class_name": "QwenImagePipeline"}))
    assert registry.checkpoints(include_invalid=True)["qwen-image-21"].reason == "unsupported_pipeline"


def test_qwen_registry_requires_every_hf_weight_shard(project):
    _, _, root = project
    model = add_qwen_checkpoint(root)
    transformer = model / "transformer"
    (transformer / "model.safetensors").unlink()
    first = "diffusion_pytorch_model-00001-of-00002.safetensors"
    second = "diffusion_pytorch_model-00002-of-00002.safetensors"
    (transformer / first).write_bytes(b"first")
    (transformer / "diffusion_pytorch_model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"a": first, "b": second}}))
    registry = Registry(root)
    assert "qwen-image-21" not in registry.checkpoints()
    assert registry.checkpoints(include_invalid=True)["qwen-image-21"].reason == "missing_weights"
    (transformer / second).write_bytes(b"second")
    fingerprint = registry.checkpoints()["qwen-image-21"].fingerprint
    cache = model / ".cache" / "huggingface"
    cache.mkdir(parents=True)
    (cache / "download.json").write_text("progress")
    assert registry.checkpoints()["qwen-image-21"].fingerprint == fingerprint


class FakeGenerator:
    def __init__(self, device):
        self.device = device

    def manual_seed(self, seed):
        self.seed = seed
        return self


class FakePipe:
    def __init__(self):
        self.calls = []
        self.events = []
        self.active = []
        self.fail_reset = False

    def to(self, device):
        self.device = device
        return self

    def enable_model_cpu_offload(self, gpu_id):
        self.offload = ("model_cpu", gpu_id)

    def enable_sequential_cpu_offload(self, gpu_id):
        self.offload = ("sequential_cpu", gpu_id)

    def unload_lora_weights(self):
        self.events.append("reset")
        if self.fail_reset:
            raise RuntimeError("reset failed")
        self.active = []

    def load_lora_weights(self, path, weight_name, adapter_name):
        self.events.append("load")

    def set_adapters(self, names, adapter_weights):
        self.events.append(("scale", adapter_weights[0]))
        self.active = names

    def get_active_adapters(self):
        return self.active

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(images=[Image.new("RGBA", (1, 1), (255, 0, 0, 0))])


def test_qwen_pipeline_defaults_cfg_and_lora_isolation(project, monkeypatch):
    _, _, root = project
    add_qwen_checkpoint(root)
    checkpoint = Registry(root).checkpoints()["qwen-image-21"]
    pipe = FakePipe()
    loaded = []
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        float16="float16", bfloat16="bfloat16", float32="float32",
        inference_mode=nullcontext, Generator=FakeGenerator))
    monkeypatch.setitem(sys.modules, "diffusers", SimpleNamespace(
        QwenImage21Pipeline=SimpleNamespace(from_pretrained=lambda *args, **kwargs: loaded.append((args, kwargs)) or pipe)))
    backend = QwenImage21Backend("cpu", "float32")
    backend.load(checkpoint)
    assert loaded[0][0] == (str(checkpoint.path),)
    assert loaded[0][1]["local_files_only"] is True
    pipe.calls.clear()  # Ignore the one-step warmup.

    base = GenerationRequest(model="qwen-image-21", prompt="test")
    styled = GenerationRequest(model="qwen-image-21", prompt="test",
                               loras=[{"lora_id": "style", "scale": 0.4}])
    lora = Lora("qwen-image-21", "style", root / "style.safetensors", "fingerprint")
    assert backend.generate(base, None, 1).startswith(b"\x89PNG")
    assert backend.generate(styled, lora, 2).startswith(b"\x89PNG")
    backend.generate(base, None, 3)
    assert pipe.events == ["reset", "reset", "load", ("scale", 0.4), "reset", "reset"]
    assert pipe.calls[0]["num_inference_steps"] == 40
    assert pipe.calls[0]["true_cfg_scale"] == 1.0
    assert pipe.calls[0]["negative_prompt"] is None
    assert pipe.calls[0]["generator"].seed == 1

    guided = GenerationRequest(model="qwen-image-21", prompt="test", guidance_scale=3.0)
    backend.generate(guided, None, 4)
    assert pipe.calls[-1]["negative_prompt"] == ""
    with pytest.raises(UnsupportedParameter):
        backend.generate(GenerationRequest(model="qwen-image-21", prompt="test",
                                           negative_prompt="bad"), None, 5)
    pipe.fail_reset = True
    with pytest.raises(CleanupFailed):
        backend.generate(base, None, 6)


@pytest.mark.parametrize("mode", ["model_cpu", "sequential_cpu"])
def test_qwen_offload_avoids_full_gpu_transfer(project, monkeypatch, mode):
    _, _, root = project
    add_qwen_checkpoint(root)
    pipe = FakePipe()
    pipe.to = lambda device: pytest.fail("full pipeline must not be moved to GPU")
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        float16="float16", bfloat16="bfloat16", float32="float32",
        inference_mode=nullcontext, Generator=FakeGenerator))
    monkeypatch.setitem(sys.modules, "diffusers", SimpleNamespace(
        QwenImage21Pipeline=SimpleNamespace(from_pretrained=lambda *args, **kwargs: pipe)))
    QwenImage21Backend("cuda:0", "bfloat16", mode).load(Registry(root).checkpoints()["qwen-image-21"])
    assert pipe.offload == (mode, 0)
