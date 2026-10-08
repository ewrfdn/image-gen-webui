"""Choose a backend from a discovered checkpoint's architecture."""

from ..config import Settings
from ..resources import Checkpoint
from .base import Backend, BackendCapabilities
from .qwen_image_21.comfy_backend import ComfyQwenImage21Backend
from .qwen_image_21.backend import QwenImage21Backend
from .sdxl.backend import SDXLBackend


CAPABILITIES = {
    "sdxl": BackendCapabilities("sdxl", 512, 1024, 64, 1_048_576, 25, 7.0),
    "qwen_image_21": BackendCapabilities("qwen_image_21", 512, 2752, 32, 4_718_592, 40, 1.0),
    "qwen_image_21_comfy": BackendCapabilities("qwen_image_21_comfy", 512, 2752, 32, 4_718_592, 40, 1.0, 0),
}


def create_backend(checkpoint: Checkpoint, settings: Settings) -> Backend:
    if checkpoint.architecture == "sdxl":
        return SDXLBackend(settings.device, settings.dtype, settings.model_offload)
    if checkpoint.architecture == "qwen_image_21":
        return QwenImage21Backend(settings.device, settings.dtype, settings.model_offload)
    if checkpoint.architecture == "qwen_image_21_comfy":
        return ComfyQwenImage21Backend(settings)
    raise ValueError(f"Unsupported model architecture: {checkpoint.architecture}")
