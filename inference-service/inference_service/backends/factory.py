"""Choose a backend from a discovered checkpoint's architecture."""

from ..config import Settings
from ..resources import Checkpoint
from .base import Backend
from .sdxl.backend import SDXLBackend


def create_backend(checkpoint: Checkpoint, settings: Settings) -> Backend:
    if checkpoint.architecture == "sdxl":
        return SDXLBackend(settings.device, settings.dtype, settings.model_offload)
    raise ValueError(f"Unsupported model architecture: {checkpoint.architecture}")
