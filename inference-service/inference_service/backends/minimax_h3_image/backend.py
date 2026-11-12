"""Native MiniMax-H3-Image inference on the official Diffusers modular pipeline.

Two placement modes share one code path:

* resident (default) — `load` brings the int8 text encoder (32 GiB), the transformer
  (62 GiB) and the fp32 VAE (2.3 GiB) onto the device and keeps them there, so a
  request only pays for sampling: ~40 s for 2048² instead of ~75 s. Large canvases
  evict the encoder before denoising and reload it lazily, because 96 GiB of weights
  plus 16 MP activations do not fit in 128 GiB.
* staged — used when the checkpoint does not fit or `H3_RESIDENT=0`; every stage loads
  only the component group it needs and frees it again (peak ~65 GiB, ~75 s).

Either way the pipeline object itself is rebuilt per stage: `update_components(name=None)`
rewrites the component specs, so a reused instance starts loading the wrong files.
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
# Above this canvas the encoder is evicted before denoising: 96 GiB of resident weights
# plus the activations of a 16 MP latent do not fit the 128 GiB unified pool.
EVICT_ENCODER_PIXELS = 8 * 1024 * 1024
GIB = 2 ** 30


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
                 encoder_int8: bool = True, resident: bool = True):
        self.capabilities = capabilities
        self.device = device
        # int8 weight-only halves the encoder (62 -> 32 GiB) at a cosine of 0.99999 on
        # the conditioning it produces, which is what lets it stay resident next to the
        # transformer.
        self.encoder_int8 = encoder_int8
        self.resident = resident
        self.loader = loader
        self.encoder_loader = encoder_loader
        self.log = log
        self.pipe = None
        self.root = None
        self.encoder = None
        self.transformer = None
        self.vae = None

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
        self.pipe = ModularPipeline.from_pretrained(str(root), local_files_only=True)
        if not self.resident:
            return
        free = torch.cuda.mem_get_info()[0] / GIB if torch.cuda.is_available() else 0.0
        if free < 96:
            self.log(f"only {free:.0f} GiB free; falling back to staged execution")
            self.resident = False
            return
        started = time.perf_counter()
        self._ensure_encoder()
        self._ensure_transformer()
        self._ensure_vae()
        torch.cuda.empty_cache()
        self.log(f"resident load {time.perf_counter() - started:.1f} s, "
                 f"{torch.cuda.memory_allocated() / GIB:.1f} GiB on device")

    def unload(self) -> None:
        import torch
        self.encoder = self.transformer = self.vae = None
        self.pipe = None
        self.root = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    # -- component cache ---------------------------------------------------
    def _ensure_encoder(self):
        if self.encoder is None:
            started = time.perf_counter()
            encoder = self.encoder_loader(self.root / "text_encoder", device=self.device,
                                          log=self.log)
            if self.encoder_int8:
                encoder = self._quantize_encoder(encoder)
            self.encoder = encoder
            self.log(f"text_encoder ready in {time.perf_counter() - started:.1f} s")
        return self.encoder

    def _ensure_transformer(self):
        import torch
        from diffusers import MiniMaxH3ImageTransformer2DModel
        if self.transformer is None:
            started = time.perf_counter()
            self.transformer = self.loader(MiniMaxH3ImageTransformer2DModel,
                                           self.root / "transformer_diffusers",
                                           device=self.device, log=self.log)
            self.log(f"transformer ready in {time.perf_counter() - started:.1f} s")
        return self.transformer

    def _ensure_vae(self):
        import torch
        from diffusers import AutoencoderKLMiniMaxH3Image
        if self.vae is None:
            self.vae = self.loader(AutoencoderKLMiniMaxH3Image, self.root / "vae_diffusers",
                                   device=self.device, dtype=torch.float32, log=self.log)
        return self.vae

    def _quantize_encoder(self, encoder):
        import torch
        from torchao.quantization import Int8WeightOnlyConfig, quantize_
        started = time.perf_counter()
        quantize_(encoder, Int8WeightOnlyConfig())
        torch.cuda.synchronize()
        # quantize_ swaps every weight for an int8 copy; the freed bf16 blocks stay in
        # the allocator cache and would otherwise hold ~17 GiB the pool needs.
        torch.cuda.empty_cache()
        self.log(f"  text_encoder int8: {time.perf_counter() - started:.1f} s, "
                 f"{torch.cuda.memory_allocated() / GIB:.1f} GiB on device")
        return encoder

    def _pipeline(self):
        from diffusers import ModularPipeline
        return ModularPipeline.from_pretrained(str(self.root), local_files_only=True)

    def _drop_encoder(self) -> None:
        import torch
        self.encoder = None
        gc.collect()
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
        # Tokenizer and processor are megabytes; they are loaded per stage in both modes.
        names = ["tokenizer", "processor"] + (["vae"] if references and not self.resident else [])
        pipe.load_components(names=names, dtype=torch.bfloat16, device_map=self.device)
        encoder = self._ensure_encoder()
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
            if not self.resident:
                self._free(pipe, names)

    def _denoise(self, payload: dict, width: int, height: int, seed: int) -> dict:
        import torch
        from diffusers.modular_pipelines.modular_pipeline import PipelineState

        # A 16 MP canvas needs the pool the encoder occupies; drop it and reload lazily.
        if self.resident and width * height > EVICT_ENCODER_PIXELS and self.encoder is not None:
            self.log(f"evicting text_encoder for {width}x{height}")
            self._drop_encoder()
        pipe = self._pipeline()
        # The scheduler is a few kilobytes; both modes load it for the stage.
        pipe.load_components(names=["scheduler"], device_map=self.device)
        transformer = self._ensure_transformer()
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
            if not self.resident:
                pipe.unload_components(["scheduler"])

    def _decode(self, payload: dict):
        import torch
        from diffusers.modular_pipelines.modular_pipeline import PipelineState

        pipe = self._pipeline()
        vae = self._ensure_vae()
        pipe.update_components(vae=vae)
        state = PipelineState()
        for key, value in payload.items():
            state.set(key, value)
        state.set("output_type", "pil")
        pipe, state = pipe.blocks.sub_blocks["decode"](pipe, state)
        images = state.get("images")
        if not images:
            raise RuntimeError("decode produced no image")
        return images[0]

    @staticmethod
    def _free(pipe, names) -> None:
        import torch
        if names:
            pipe.unload_components(list(names))
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
