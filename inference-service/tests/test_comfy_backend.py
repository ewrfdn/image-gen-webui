import json
from pathlib import Path

import pytest

from inference_service.backends.factory import create_backend
from inference_service.backends.qwen_image_21.comfy_backend import ComfyQwenImage21Backend
from inference_service.config import Settings
from inference_service.engine.registry import Registry
from inference_service.resources import QWEN_COMFY_FILES, QWEN_COMFY_MODEL_ID
from inference_service.schemas import GenerationRequest


def test_comfy_layout_is_discovered_only_when_complete(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    first = root / next(iter(QWEN_COMFY_FILES.values()))
    first.parent.mkdir()
    first.write_bytes(b"weights")
    registry = Registry(root)
    assert QWEN_COMFY_MODEL_ID not in registry.checkpoints()
    assert set(registry.checkpoints(include_invalid=True)) == {QWEN_COMFY_MODEL_ID}
    assert registry.checkpoints(include_invalid=True)[QWEN_COMFY_MODEL_ID].reason == "missing_comfy_component"
    for relative in list(QWEN_COMFY_FILES.values())[1:]:
        path = root / relative
        path.parent.mkdir()
        path.write_bytes(b"weights")
    checkpoint = registry.checkpoints()[QWEN_COMFY_MODEL_ID]
    assert checkpoint.architecture == "qwen_image_21_comfy"
    assert checkpoint.public()["format"] == "comfyui"
    assert registry.loras(QWEN_COMFY_MODEL_ID) == []
    assert isinstance(create_backend(checkpoint, Settings(root, "infer", "admin")), ComfyQwenImage21Backend)


def test_private_comfy_process_load_generate_unload(tmp_path, monkeypatch):
    comfy = tmp_path / "ComfyUI"
    root = comfy / "models"
    python = tmp_path / "python"
    comfy.mkdir()
    root.mkdir()
    (comfy / "main.py").touch()
    python.touch()
    for relative in QWEN_COMFY_FILES.values():
        source = root / relative
        source.parent.mkdir()
        source.write_bytes(b"weights")
    checkpoint = Registry(root).checkpoints()[QWEN_COMFY_MODEL_ID]
    backend = ComfyQwenImage21Backend(Settings(root, "infer", "admin", comfyui_root=comfy, comfyui_python=python))
    events = []

    class Process:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            events.append("terminate")
            self.stopped = True

        def wait(self, timeout):
            return 0

    def start(args, **kwargs):
        events.append((args, kwargs))
        return Process()

    monkeypatch.setattr("inference_service.backends.qwen_image_21.comfy_backend.subprocess.Popen", start)
    image = b"\x89PNG\r\n\x1a\n" + b"fake"
    graphs = []

    def request(route, payload=None, timeout=10):
        if route == "/system_stats":
            return b"{}"
        if route == "/prompt":
            graphs.append(payload["prompt"])
            return b'{"prompt_id":"test"}'
        if route == "/history/test":
            return json.dumps({"test": {"outputs": {"8": {"images": [
                {"filename": "result_00001_.png", "subfolder": "", "type": "output"}]}}}}).encode()
        if route.startswith("/view?"):
            return image
        raise AssertionError(route)

    monkeypatch.setattr(backend, "_request", request)
    backend.load(checkpoint)
    output = backend.generate(GenerationRequest(model=QWEN_COMFY_MODEL_ID, prompt="landscape", size="1024x1024"), None, 42)
    assert output == image
    assert graphs[0]["6"]["inputs"]["steps"] == 1
    assert graphs[1]["4"]["inputs"]["prompt"] == "landscape"
    assert graphs[1]["6"]["inputs"]["seed"] == 42
    assert graphs[1]["6"]["inputs"]["steps"] == 40
    args, kwargs = events[0]
    assert "--disable-all-custom-nodes" in args
    assert "127.0.0.1" in args
    assert "8188" not in args
    assert kwargs["cwd"] == comfy
    private_workspace = Path(backend.workspace.name)
    backend.unload()
    assert events[1] == "terminate"
    assert not private_workspace.exists()


def test_negative_prompt_requires_cfg():
    backend = ComfyQwenImage21Backend(Settings(Path("."), "infer", "admin"))
    request = GenerationRequest(model=QWEN_COMFY_MODEL_ID, prompt="scene", negative_prompt="blur")
    with pytest.raises(Exception, match="guidance_scale > 1"):
        backend.generate(request, None, 1)
