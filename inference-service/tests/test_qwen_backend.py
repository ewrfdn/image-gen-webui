"""Qwen-Image 2.1 discovery and pipeline behavior without GPU dependencies."""

from contextlib import nullcontext
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from inference_service.backends.base import CleanupFailed, LoraIncompatible, UnsupportedParameter
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
        self.transformer = SimpleNamespace(get_submodule=self.get_submodule)

    def get_submodule(self, name):
        if name != "attn.to_q":
            raise AttributeError(name)
        return SimpleNamespace(weight=SimpleNamespace(shape=(4, 3)))

    def lora_state_dict(self, *args, **kwargs):
        return {"transformer.attn.to_q.lora_A.weight": SimpleNamespace(shape=(2, 3)),
                "transformer.attn.to_q.lora_B.weight": SimpleNamespace(shape=(4, 2))}

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

    def load_lora_weights(self, path, weight_name=None, adapter_name=None):
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


def test_qwen_failed_gpu_transfer_keeps_pipeline_for_cleanup(project, monkeypatch):
    _, _, root = project
    add_qwen_checkpoint(root)
    pipe = FakePipe()

    def fail_transfer(device):
        raise RuntimeError("CUDA out of memory")

    pipe.to = fail_transfer
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        float16="float16", bfloat16="bfloat16", float32="float32",
        inference_mode=nullcontext, Generator=FakeGenerator,
        cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "diffusers", SimpleNamespace(
        QwenImage21Pipeline=SimpleNamespace(from_pretrained=lambda *args, **kwargs: pipe)))
    backend = QwenImage21Backend("cuda:0", "bfloat16")
    with pytest.raises(RuntimeError, match="out of memory"):
        backend.load(Registry(root).checkpoints()["qwen-image-21"])
    assert backend.pipeline is pipe
    backend.unload()
    assert backend.pipeline is None


@pytest.mark.parametrize("bad_key,shape", [("unknown.lora_B.weight", (4, 2)), ("attn.to_q.lora_B.weight", (5, 2))])
def test_qwen_rejects_partial_or_wrong_shape_lora(monkeypatch, tmp_path, bad_key, shape):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    pipe = FakePipe()
    pipe.lora_state_dict = lambda *args, **kwargs: {
        "transformer.attn.to_q.lora_A.weight": SimpleNamespace(shape=(2, 3)),
        "transformer." + bad_key: SimpleNamespace(shape=shape)}
    backend = QwenImage21Backend("cpu", "float32")
    backend.pipeline = pipe
    request = GenerationRequest(model="qwen", prompt="test", loras=[{"lora_id": "style"}])
    with pytest.raises(LoraIncompatible):
        backend.generate(request, Lora("qwen", "style", tmp_path / "style.safetensors", "test"), 1)
    assert "load" not in pipe.events
    assert pipe.events == ["reset", "reset"]
    assert pipe.calls == []


@pytest.mark.parametrize("architecture", ["qwen", "sdxl"])
def test_multiple_loras_apply_together_and_reset(tmp_path, monkeypatch, architecture):
    from inference_service.backends.sdxl.backend import SDXLBackend
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext, Generator=FakeGenerator))
    pipe = FakePipe()
    applied = []
    original = pipe.set_adapters
    def capture(names, adapter_weights):
        applied.append((list(names), list(adapter_weights)))
        original(names, adapter_weights)
    pipe.set_adapters = capture
    backend = QwenImage21Backend("cpu", "float32") if architecture == "qwen" else SDXLBackend("cpu", "float32")
    backend.pipeline = pipe
    loras = [Lora("style-a", "model", tmp_path / "a.safetensors", "a"), Lora("style-b", "model", tmp_path / "b.safetensors", "b")]
    request = GenerationRequest(model="model", prompt="a", loras=[{"lora_id": "style-a", "scale": .5}, {"lora_id": "style-b", "scale": .8}])
    result = backend.generate(request, loras, 1)
    assert result.startswith(b"\x89PNG")
    assert applied == [(["request_0", "request_1"], [.5, .8])]
    assert pipe.active == [] and pipe.events[-1] == "reset"


@pytest.mark.parametrize("count", [1, 10])
def test_qwen_reference_images_are_decoded_and_forwarded(monkeypatch, count):
    import base64, io
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext, Generator=FakeGenerator))
    out = io.BytesIO()
    Image.new("RGBA", (16, 16), (25, 50, 100, 128)).save(out, format="PNG")
    encoded = base64.b64encode(out.getvalue()).decode()
    backend = QwenImage21Backend("cpu", "float32")
    backend.pipeline = FakePipe()
    request = GenerationRequest(model="qwen", prompt="keep the subject", image=[encoded] * count)
    assert backend.generate(request, None, 1).startswith(b"\x89PNG")
    images = backend.pipeline.calls[-1]["image"]
    assert len(images) == count
    assert all(img.mode == "RGBA" and img.getpixel((0, 0)) == (25, 50, 100, 128) for img in images)
    backend.generate(GenerationRequest(model="qwen", prompt="no references"), None, 2)
    assert "image" not in backend.pipeline.calls[-1]


def test_qwen_rejects_invalid_reference_pixels(monkeypatch):
    import base64
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext, Generator=FakeGenerator))
    backend = QwenImage21Backend("cpu", "float32")
    backend.pipeline = FakePipe()
    with pytest.raises(UnsupportedParameter):
        backend.generate(GenerationRequest(model="qwen", prompt="a", image=[base64.b64encode(b"not an image").decode()]), None, 1)
    assert backend.pipeline.calls == []
