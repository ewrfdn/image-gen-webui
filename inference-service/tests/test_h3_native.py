import json
from pathlib import Path

import pytest

from inference_service.backends.base import LoraIncompatible, UnsupportedParameter
from inference_service.backends.factory import CAPABILITIES, create_backend
from inference_service.backends.minimax_h3_image.backend import MiniMaxH3ImageBackend
from inference_service.config import Settings
from inference_service.engine.registry import Registry
from inference_service.schemas import GenerationRequest

CAPS = CAPABILITIES["minimax_h3_image"]


def modular_checkpoint(tmp_path: Path):
    root = tmp_path / "models"
    directory = root / "h3-full"
    directory.mkdir(parents=True)
    (directory / "modular_model_index.json").write_text(json.dumps({
        "_class_name": "MiniMaxH3ImageModularPipeline",
        "_blocks_class_name": "MiniMaxH3ImageBlocks",
        "sample_sigmas": [1.0, 0.5, 0.0],
    }))
    for subfolder in ("text_encoder", "transformer_diffusers", "vae_diffusers"):
        part = directory / subfolder
        part.mkdir()
        (part / "config.json").write_text("{}")
    return root, directory


def test_modular_layout_is_discovered(tmp_path):
    root, _ = modular_checkpoint(tmp_path)
    checkpoint = Registry(root).checkpoints()["h3-full"]
    assert checkpoint.layout == "modular"
    assert checkpoint.architecture == "minimax_h3_image"
    assert checkpoint.public()["format"] == "diffusers-modular"


def test_incomplete_modular_checkpoint_is_skipped(tmp_path):
    root, directory = modular_checkpoint(tmp_path)
    (directory / "vae_diffusers" / "config.json").unlink()
    assert Registry(root).checkpoints() == {}


def test_factory_selects_native_backend_for_modular(tmp_path):
    root, _ = modular_checkpoint(tmp_path)
    checkpoint = Registry(root).checkpoints()["h3-full"]
    settings = Settings(root, "i", "a", "cpu", "float32")
    assert isinstance(create_backend(checkpoint, settings), MiniMaxH3ImageBackend)


def test_native_backend_rejects_unsupported_parameters(tmp_path):
    _, directory = modular_checkpoint(tmp_path)
    backend = MiniMaxH3ImageBackend(CAPS, device="cpu")
    backend.root = directory
    backend.pipe = object()
    for request, error in (
        (GenerationRequest(model="m", prompt="p", size="2048x2048", num_inference_steps=3), UnsupportedParameter),
        (GenerationRequest(model="m", prompt="p", size="2048x2048", guidance_scale=2.0), UnsupportedParameter),
        (GenerationRequest(model="m", prompt="p", size="2048x2048", negative_prompt="x"), UnsupportedParameter),
    ):
        with pytest.raises(error):
            backend.generate(request, None, 1)


def test_native_backend_rejects_lora(tmp_path):
    _, directory = modular_checkpoint(tmp_path)
    backend = MiniMaxH3ImageBackend(CAPS, device="cpu")
    backend.root = directory
    backend.pipe = object()
    with pytest.raises(LoraIncompatible):
        backend.generate(GenerationRequest(model="m", prompt="p", size="2048x2048"), "lora", 1)


def test_load_requires_modular_index(tmp_path):
    _, directory = modular_checkpoint(tmp_path)
    (directory / "modular_model_index.json").unlink()
    from types import SimpleNamespace

    checkpoint = SimpleNamespace(path=directory)
    with pytest.raises(ValueError, match="MODULAR_MODEL_INDEX_MISSING"):
        MiniMaxH3ImageBackend(CAPS, device="cpu").load(checkpoint)
