"""Choose a backend from a discovered checkpoint's architecture."""

from ..config import Settings
from ..resources import Checkpoint
from .base import Backend, BackendCapabilities
from .comfyui.backend import ComfyUIBackend
from .minimax_h3_image.backend import MiniMaxH3ImageBackend
from .qwen_image_21.backend import QwenImage21Backend
from .sdxl.backend import SDXLBackend


CAPABILITIES = {
    "sdxl": BackendCapabilities("sdxl", 512, 1024, 64, 1_048_576, 25, 7.0),
    "qwen_image_21": BackendCapabilities("qwen_image_21", 512, 2752, 32, 4_718_592, 40, 1.0, reference_max_count=10),
    # MiniMax-H3-Image is distilled to a fixed six-step grid with guidance baked
    # into the weights: no CFG, no negative prompt and no tunable step count.
    "minimax_h3_image": BackendCapabilities(
        "minimax_h3_image", 1024, 6272, 32, 16_859_136, 6, 1.0,
        reference_max_count=10, fixed_steps=6, default_size="3104x3104", supports_guidance=False,
        supports_negative_prompt=False, lora_limit=0, max_count=2),
}


def create_backend(checkpoint: Checkpoint, settings: Settings) -> Backend:
    if checkpoint.architecture == "sdxl":
        return SDXLBackend(settings.device, settings.dtype, settings.model_offload)
    if checkpoint.architecture == "qwen_image_21":
        return QwenImage21Backend(settings.device, settings.dtype, settings.model_offload)
    if checkpoint.architecture == "minimax_h3_image":
        if checkpoint.layout == "modular":
            # Full-precision Diffusers checkpoint: staged, in-process execution.
            return MiniMaxH3ImageBackend(CAPABILITIES["minimax_h3_image"], settings.device)
        return ComfyUIBackend(CAPABILITIES["minimax_h3_image"], settings.comfyui_url,
                              settings.comfyui_timeout_seconds, settings.comfyui_warmup)
    raise ValueError(f"Unsupported model architecture: {checkpoint.architecture}")
