"""Native MiniMax-H3-Image inference on the official Diffusers modular pipeline.

The full-precision checkpoint is 135 GB (62 GB transformer, 63 GB Qwen3-VL text
encoder, 2.2 GB VAE) and one DGX Spark has 128 GB of unified memory, so the backend
never holds the pipeline: every request loads one component group, runs its stage and
frees it again. Peak residency is therefore ~65 GB instead of 135 GB.

    encode   text_encoder + tokenizer + processor (+ vae for edit references)  ~14 s
    denoise  transformer + scheduler                                          ~31 s
    decode   vae (fp32) + image processor                                     ~5 s

Weights stay in the page cache between requests, so the second request is faster.
"""

import base64
import gc
import io
import logging
import time

from ...resources import Checkpoint, Lora
from ...schemas import GenerationRequest
from ..base import BackendCapabilities, LoraIncompatible, UnsupportedParameter
from .loading import load_diffusers_gpu, load_qwen3vl

LOGGER = logging.getLogger(__name__)

# `before_denoise` builds its row-index tensors on the CPU and moves them afterwards, so
# `text_token_tags` must stay on the CPU; every other staged tensor is a model input.
CPU_KEYS = {"text_token_tags"}
STATE_KEYS = ("prompt_embeds", "text_token_tags", "reference_latents", "reference_noise",
              "latents", "height", "width", "output_height", "output_width",
              "min_generation_area")


def move_to_device(value, device="cuda", key=None):
    import torch
    if torch.is_tensor(value):
        return value if key in CPU_KEYS else value.to(device)
    if isinstance(value, dict):
        return {name: move_to_device(item, device, name) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        moved = [move_to_device(item, device, key) for item in value]
        return type(value)(moved) if isinstance(value, tuple) else moved
    return value


class MiniMaxH3ImageBackend:
    def __init__(self, capabilities: BackendCapabilities, device: str = "cuda",
                 loader=load_diffusers_gpu, encoder_loader=load_qwen3vl, log=LOGGER.info,
                 encoder_int8: bool = True):
        self.capabilities = capabilities
        self.device = device
        # int8 weight-only halves the encoder (62 -> 32 GiB) at a cosine of 0.99999 on
        # the conditioning it produces, which is what lets it stay resident next to the
        # transformer instead of being reloaded for every request.
        self.encoder_int8 = encoder_int8
        self.loader = loader
        self.encoder_loader = encoder_loader
        self.log = log
        self.pipe = None
        self.root = None

    # -- lifecycle ---------------------------------------------------------
    def load(self, checkpoint: Checkpoint) -> None:
        import torch
        from diffusers import ModularPipeline

        root = checkpoint.path
        if not (root / "modular_model_index.json").is_file():
            raise ValueError("MODULAR_MODEL_INDEX_MISSING")
        for subfolder in ("text_encoder", "transformer_diffusers", "vae_diffusers"):
            if not (root / subfolder).is_dir() or not any((root / subfolder).iterdir()):
                raise ValueError(f"MODEL_COMPONENT_MISSING:{subfolder}")
        self.root = root
        # A fresh ModularPipeline per stage: `update_components(name=None)` rewrites the
        # component specs, and a reused pipeline then loads the wrong component files.
        self.pipe = self._pipeline()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _quantize_encoder(self, encoder):
        import torch
        from torchao.quantization import Int8WeightOnlyConfig, quantize_
        started = time.perf_counter()
        quantize_(encoder, Int8WeightOnlyConfig())
        torch.cuda.synchronize()
        self.log(f"  text_encoder int8: {time.perf_counter() - started:.1f} s, "
                 f"{torch.cuda.memory_allocated() / 2**30:.1f} GiB on device")
        return encoder

    def _pipeline(self):
        from diffusers import ModularPipeline
        return ModularPipeline.from_pretrained(str(self.root), local_files_only=True)

    def unload(self) -> None:
        import torch
        self.pipe = None
        self.root = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    # -- generation --------------------------------------------------------
    def generate(self, request: GenerationRequest, lora: Lora | list[Lora] | None, seed: int) -> bytes:
        if self.pipe is None or self.root is None:
            raise RuntimeError("pipeline is not loaded")
        self._validate(request, lora)
        references = self._references(request)
        width, height = (int(part) for part in request.size.split("x"))
        state = self._encode(request, references, width, height, seed)
        state = self._denoise(state, width, height, seed)
        image = self._decode(state)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def _validate(self, request: GenerationRequest, lora) -> None:
        if lora is not None:
            raise LoraIncompatible("This architecture loads no LoRA adapters")
        caps = self.capabilities
        if request.negative_prompt and not caps.supports_negative_prompt:
            raise UnsupportedParameter("negative_prompt is not supported by this model")
        if caps.fixed_steps is not None and request.num_inference_steps not in (None, caps.fixed_steps):
            raise UnsupportedParameter(f"num_inference_steps must be {caps.fixed_steps} for this model")
        if not caps.supports_guidance and request.guidance_scale not in (None, caps.default_guidance):
            raise UnsupportedParameter(
                f"guidance_scale must be {caps.default_guidance} for this model")
        if len(request.image) > caps.reference_max_count:
            raise UnsupportedParameter("Reference count exceeds this model backend's capability")

    @staticmethod
    def _references(request: GenerationRequest) -> list:
        from PIL import Image
        images = []
        for encoded in request.image:
            try:
                data = base64.b64decode(encoded, validate=True)
                if len(data) > 20 * 1024 * 1024:
                    raise ValueError("reference exceeds 20 MB")
                with Image.open(io.BytesIO(data)) as image:
                    if image.format not in ("PNG", "JPEG", "WEBP"):
                        raise ValueError("unsupported reference format")
                    images.append(image.convert("RGB"))
            except Exception as error:
                raise UnsupportedParameter(
                    "Reference must be a valid PNG, JPEG or WebP, at most 20 MB") from error
        return images

    def _encode(self, request, references, width, height, seed) -> dict:
        import torch
        from diffusers.modular_pipelines.modular_pipeline import PipelineState

        pipe = self._pipeline()
        names = ["tokenizer", "processor"] + (["vae"] if references else [])
        pipe.load_components(names=names, dtype=torch.bfloat16, device_map=self.device)
        encoder = self.encoder_loader(self.root / "text_encoder", device=self.device, log=self.log)
        if self.encoder_int8:
            encoder = self._quantize_encoder(encoder)
        pipe.update_components(text_encoder=encoder)
        try:
            state = PipelineState()
            state.set("prompt", request.prompt)
            state.set("height", height)
            state.set("width", width)
            state.set("min_generation_area", 4194304)
            state.set("generator", torch.Generator("cpu").manual_seed(seed))
            if references:
                state.set("image", references if len(references) > 1 else references[0])
            pipe, state = pipe.blocks.sub_blocks["before_encode"](pipe, state)
            pipe, state = pipe.blocks.sub_blocks["text_encoder"](pipe, state)
            if references:
                pipe, state = pipe.blocks.sub_blocks["vae_encoder"](pipe, state)
            return {key: state.get(key) for key in STATE_KEYS if state.get(key) is not None}
        finally:
            del encoder
            self._free(pipe, names)

    def _denoise(self, payload: dict, width: int, height: int, seed: int) -> dict:
        import torch
        from diffusers import MiniMaxH3ImageTransformer2DModel
        from diffusers.modular_pipelines.modular_pipeline import PipelineState

        pipe = self._pipeline()
        pipe.load_components(names=["scheduler"], device_map=self.device)
        transformer = self.loader(MiniMaxH3ImageTransformer2DModel,
                                  self.root / "transformer_diffusers", device=self.device,
                                  log=self.log)
        pipe.update_components(transformer=transformer)
        try:
            state = PipelineState()
            for key, value in move_to_device(payload, self.device).items():
                state.set(key, value)
            state.set("generator", torch.Generator("cpu").manual_seed(seed))
            state.set("height", height)
            state.set("width", width)
            state.set("min_generation_area", 4194304)
            pipe, state = pipe.blocks.sub_blocks["denoise"](pipe, state)
            return {key: state.get(key) for key in ("latents", "output_height", "output_width")}
        finally:
            del transformer
            self._free(pipe, ["scheduler"])

    def _decode(self, payload: dict):
        import torch
        from diffusers import AutoencoderKLMiniMaxH3Image
        from diffusers.modular_pipelines.modular_pipeline import PipelineState

        pipe = self._pipeline()
        # fp32 decode and the fast shard loader: the VAE is only 2.2 GB, but diffusers'
        # own loader needs over two minutes for it on this machine. The decoder takes
        # latents_mean / latents_std from `vae.config`, which from_config keeps.
        vae = self.loader(AutoencoderKLMiniMaxH3Image, self.root / "vae_diffusers",
                          device=self.device, dtype=torch.float32, log=self.log)
        pipe.update_components(vae=vae)
        try:
            state = PipelineState()
            for key, value in payload.items():
                state.set(key, value)
            state.set("output_type", "pil")
            pipe, state = pipe.blocks.sub_blocks["decode"](pipe, state)
            images = state.get("images")
            if not images:
                raise RuntimeError("decode produced no image")
            return images[0]
        finally:
            del vae
            self._free(pipe, [])

    @staticmethod
    def _free(pipe, names) -> None:
        import torch
        if names:
            pipe.unload_components(list(names))
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
