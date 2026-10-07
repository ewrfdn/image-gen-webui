from contextlib import nullcontext
import shutil
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from inference_service.backends import CleanupFailed, SDXLBackend
from inference_service.registry import Lora, Registry
from inference_service.schemas import GenerationRequest


def test_direct_diffusers_layout_keeps_global_loras_unassigned(project, tmp_path):
    _, _, root = project
    direct = root / "direct-model"
    shutil.copytree(root / "sdxl-base" / "model", direct)
    (root / "loras").mkdir()
    shutil.copy(root / "sdxl-base" / "loras" / "style.safetensors", root / "loras" / "style.safetensors")
    catalog = Registry(root).checkpoints()
    assert catalog["direct-model"].layout == "direct"
    assert Registry(root).loras("direct-model") == []


def test_sdxl_backend_resets_adapter_on_every_request(project, monkeypatch):
    _, _, root = project

    class FakeGenerator:
        def __init__(self, device):
            pass

        def manual_seed(self, seed):
            return self

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext,
                                                               Generator=FakeGenerator))

    class FakePipe:
        def __init__(self):
            self.events = []
            self.fail_reset_at = None
            self.active = []

        def unload_lora_weights(self):
            self.events.append("reset")
            if self.fail_reset_at == self.events.count("reset"):
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
            self.events.append("generate")
            return SimpleNamespace(images=[Image.new("RGB", (1, 1))])

    pipe = FakePipe()
    backend = SDXLBackend("cpu", "float32")
    backend.pipeline = pipe
    base = GenerationRequest(model="sdxl-base", prompt="test")
    styled = GenerationRequest(model="sdxl-base", prompt="test", loras=[{"lora_id": "style", "scale": 0.3}])
    lora = Lora("sdxl-base", "style", root / "sdxl-base" / "loras" / "style.safetensors", "fingerprint")
    backend.generate(base, None, 1)
    backend.generate(styled, lora, 2)
    backend.generate(base, None, 3)
    assert pipe.events == ["reset", "generate", "reset", "load", ("scale", 0.3),
                           "generate", "reset", "reset", "generate"]

    pipe.fail_reset_at = 5
    with pytest.raises(CleanupFailed):
        backend.generate(base, None, 4)


def test_offload_modes_avoid_full_pipeline_to_gpu(project, monkeypatch):
    app, _, root = project

    class FakeGenerator:
        def __init__(self, device):
            pass

        def manual_seed(self, seed):
            return self

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        float16="float16", bfloat16="bfloat16", float32="float32",
        inference_mode=nullcontext, Generator=FakeGenerator))

    class FakePipe:
        offloaded = None

        def to(self, device):
            raise AssertionError("full pipeline must not be moved to GPU")

        def enable_model_cpu_offload(self, gpu_id):
            assert gpu_id == 0
            self.offloaded = "model_cpu"

        def enable_sequential_cpu_offload(self, gpu_id):
            assert gpu_id == 0
            self.offloaded = "sequential_cpu"

        def __call__(self, **kwargs):
            return SimpleNamespace(images=[Image.new("RGB", (1, 1))])

    for mode in ("model_cpu", "sequential_cpu"):
        pipe = FakePipe()
        monkeypatch.setitem(sys.modules, "diffusers", SimpleNamespace(
            StableDiffusionXLPipeline=SimpleNamespace(from_pretrained=lambda *args, **kwargs: pipe)))
        backend = SDXLBackend("cuda:0", "bfloat16", mode)
        backend.load(Registry(root).checkpoints()["sdxl-base"])
        assert pipe.offloaded == mode
