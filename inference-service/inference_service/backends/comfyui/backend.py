"""ComfyUI-backed inference for single-file checkpoints Diffusers cannot load.

MiniMax-H3-Image ships as ComfyUI weights (pruned DiT + quantized Qwen3-VL text
encoder + VAE), which is the only form that fits a single DGX Spark. This backend
keeps the inference-service contract: it validates the request against the
architecture capabilities and drives a local ComfyUI server through its HTTP API.
"""

import base64
import io
import json
import logging
import time

from ...resources import Checkpoint, Lora
from ...schemas import GenerationRequest
from ..base import BackendCapabilities, LoraIncompatible, UnsupportedParameter
from .client import ComfyUIClient, ComfyUIUnavailable

LOGGER = logging.getLogger(__name__)

# Workflow node per architecture; each conditions on the prompt and emits positive + latent.
CONDITIONING_NODES = {"minimax_h3_image": "MiniMaxH3ImageConditioning"}
BASE_NODES = ("UNETLoader", "CLIPLoader", "VAELoader", "KSampler", "VAEDecode", "ConditioningZeroOut")
DESCRIPTOR = "comfy-model.json"
_REFERENCE_MIME = ("PNG", "JPEG", "WEBP")


def read_descriptor(checkpoint: Checkpoint) -> dict:
    path = checkpoint.resource_dir / DESCRIPTOR
    if not path.is_file():
        raise ValueError(f"{DESCRIPTOR} is missing")
    descriptor = json.loads(path.read_text(encoding="utf-8"))
    if descriptor.get("schema_version") != 1:
        raise ValueError(f"{DESCRIPTOR} schema_version must be 1")
    if descriptor.get("architecture") != checkpoint.architecture:
        raise ValueError(f"{DESCRIPTOR} architecture does not match the discovered checkpoint")
    if descriptor.get("backend") != "comfyui":
        raise ValueError(f"{DESCRIPTOR} backend must be comfyui")
    if descriptor.get("architecture") not in CONDITIONING_NODES:
        raise ValueError(f"{descriptor.get('architecture')} has no ComfyUI workflow")
    return descriptor


class ComfyUIBackend:
    def __init__(self, capabilities: BackendCapabilities, base_url: str,
                 timeout: float = 900.0, warmup: bool = False,
                 client: ComfyUIClient | None = None):
        self.capabilities = capabilities
        self.client = client or ComfyUIClient(base_url, timeout)
        self.warmup = warmup
        self.descriptor: dict | None = None

    # -- lifecycle ---------------------------------------------------------
    def load(self, checkpoint: Checkpoint) -> None:
        descriptor = read_descriptor(checkpoint)
        node = CONDITIONING_NODES[checkpoint.architecture]
        info = self.client.object_info(node, *BASE_NODES)
        models = descriptor["models"]
        available = {
            "unet": self.client.choice_values(info["UNETLoader"], "unet_name"),
            "clip": self.client.choice_values(info["CLIPLoader"], "clip_name"),
            "vae": self.client.choice_values(info["VAELoader"], "vae_name"),
        }
        for key, name in models.items():
            if key in available and name not in available[key]:
                raise ComfyUIUnavailable(
                    f"ComfyUI does not expose {key} file {name}; "
                    f"add it to the ComfyUI model folders and restart ComfyUI")
        sampling = descriptor.get("sampling") or {}
        if self.capabilities.fixed_steps is not None:
            if sampling.get("steps") != self.capabilities.fixed_steps:
                raise ValueError(
                    f"{DESCRIPTOR} sampling.steps must be {self.capabilities.fixed_steps} for this architecture")
        self.descriptor = descriptor
        if self.warmup:
            # ComfyUI loads weights lazily, so without this the first real request
            # pays for ~57 GB of disk reads and usually exceeds the caller's wait.
            request = GenerationRequest(model=checkpoint.model_id, prompt="warmup",
                                        size=str(self.capabilities.min_size) + "x"
                                        + str(self.capabilities.min_size))
            self.generate(request, None, 0)

    def unload(self) -> None:
        self.descriptor = None
        try:
            self.client.free()
        except ComfyUIUnavailable as error:
            # Unload is idempotent: a stopped ComfyUI already holds no model.
            LOGGER.warning("ComfyUI free failed: %s", error)

    # -- generation --------------------------------------------------------
    def generate(self, request: GenerationRequest, lora: Lora | list[Lora] | None, seed: int) -> bytes:
        descriptor = self.descriptor
        if descriptor is None:
            raise RuntimeError("backend is not loaded")
        self._validate(request, lora)
        width, height = (int(part) for part in request.size.split("x"))
        sampling = descriptor.get("sampling") or {}
        graph = self._graph(descriptor, request, width, height, seed, sampling)
        for index, encoded in enumerate(request.image, start=1):
            png = self._reference_png(encoded)
            name = self.client.upload_image(f"image-gen-{index}.png", png)
            graph[f"load{index}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
            graph["conditioning"]["inputs"][f"image_{index}"] = [f"load{index}", 0]
        prompt_id = self.client.enqueue(graph)
        entry = self.client.wait(prompt_id, time.monotonic() + self.client.timeout)
        filename, subfolder = self._first_output(entry)
        png = self.client.image(filename, subfolder)
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("ComfyUI output is not a PNG")
        return png

    def _validate(self, request: GenerationRequest, lora: Lora | list[Lora] | None) -> None:
        if lora is not None:
            raise LoraIncompatible("This architecture loads no LoRA adapters")
        if request.negative_prompt and not self.capabilities.supports_negative_prompt:
            raise UnsupportedParameter("negative_prompt is not supported by this model")
        steps = self.capabilities.fixed_steps
        if steps is not None and request.num_inference_steps not in (None, steps):
            raise UnsupportedParameter(f"num_inference_steps must be {steps} for this model")
        if not self.capabilities.supports_guidance:
            if request.guidance_scale is not None and request.guidance_scale != self.capabilities.default_guidance:
                raise UnsupportedParameter(
                    f"guidance_scale must be {self.capabilities.default_guidance} for this model")
        if len(request.image) > self.capabilities.reference_max_count:
            raise UnsupportedParameter("Reference count exceeds this model backend's capability")

    def _graph(self, descriptor: dict, request: GenerationRequest,
               width: int, height: int, seed: int, sampling: dict) -> dict:
        models = descriptor["models"]
        node = CONDITIONING_NODES[descriptor["architecture"]]
        return {
            "unet": {"class_type": "UNETLoader",
                     "inputs": {"unet_name": models["unet"], "weight_dtype": "default"}},
            "clip": {"class_type": "CLIPLoader",
                     "inputs": {"clip_name": models["clip"],
                                "type": descriptor.get("clip_type", "minimax"),
                                "device": "default"}},
            "vae": {"class_type": "VAELoader", "inputs": {"vae_name": models["vae"]}},
            "conditioning": {"class_type": node,
                             "inputs": {"clip": ["clip", 0], "vae": ["vae", 0],
                                        "prompt": request.prompt,
                                        "width": width, "height": height}},
            "negative": {"class_type": "ConditioningZeroOut",
                         "inputs": {"conditioning": ["conditioning", 0]}},
            "sampler": {"class_type": "KSampler",
                        "inputs": {"model": ["unet", 0],
                                   "positive": ["conditioning", 0],
                                   "negative": ["negative", 0],
                                   "latent_image": ["conditioning", 1],
                                   "seed": int(seed),
                                   "steps": int(sampling.get("steps", self.capabilities.default_steps)),
                                   "cfg": float(sampling.get("cfg", 1.0)),
                                   "sampler_name": sampling.get("sampler_name", "euler"),
                                   "scheduler": sampling.get("scheduler", "simple"),
                                   "denoise": 1.0}},
            "decode": {"class_type": "VAEDecode",
                       "inputs": {"samples": ["sampler", 0], "vae": ["vae", 0]}},
            "save": {"class_type": "SaveImage",
                     "inputs": {"images": ["decode", 0], "filename_prefix": "image-gen"}},
        }

    @staticmethod
    def _reference_png(encoded: str) -> bytes:
        from PIL import Image
        try:
            data = base64.b64decode(encoded, validate=True)
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("reference exceeds 20 MB")
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in _REFERENCE_MIME:
                    raise ValueError("unsupported reference format")
                output = io.BytesIO()
                image.convert("RGB").save(output, format="PNG")
                return output.getvalue()
        except Exception as error:
            raise UnsupportedParameter(
                "Reference must be a valid PNG, JPEG or WebP, at most 20 MB") from error

    @staticmethod
    def _first_output(entry: dict) -> tuple[str, str]:
        for output in (entry.get("outputs") or {}).values():
            for image in output.get("images") or []:
                filename = image.get("filename")
                if filename:
                    return filename, image.get("subfolder") or ""
        raise RuntimeError("ComfyUI returned no image")
