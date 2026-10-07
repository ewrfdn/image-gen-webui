"""GPU backend, imported lazily so protocol tests run without CUDA packages."""

import gc
from pathlib import Path
from typing import Any

from .registry import Checkpoint, Lora
from .schemas import GenerationRequest


class CleanupFailed(Exception):
    pass


class LoraIncompatible(Exception):
    pass


class SDXLBackend:
    def __init__(self, device: str, dtype: str, model_offload: str = "none"):
        self.device = device
        self.dtype = dtype
        self.model_offload = model_offload
        self.pipeline: Any = None

    def load(self, checkpoint: Checkpoint) -> None:
        import torch
        from diffusers import StableDiffusionXLPipeline

        dtype = {"float16": torch.float16, "bfloat16": torch.bfloat16,
                 "float32": torch.float32}[self.dtype]
        pipe = StableDiffusionXLPipeline.from_pretrained(
            str(checkpoint.path), torch_dtype=dtype, use_safetensors=True,
            local_files_only=True)
        if self.model_offload == "model_cpu":
            pipe.enable_model_cpu_offload(gpu_id=int(self.device.split(":", 1)[1]))
            self.pipeline = pipe
        else:
            self.pipeline = pipe.to(self.device)
        with torch.inference_mode():
            self.pipeline(prompt="warmup", width=512, height=512,
                          num_inference_steps=1, guidance_scale=0.0,
                          generator=torch.Generator(device=self.device).manual_seed(0))

    def generate(self, request: GenerationRequest, lora: Lora | None, seed: int) -> bytes:
        import io
        import torch

        pipe = self.pipeline
        if pipe is None:
            raise RuntimeError("pipeline is not loaded")
        adapter_attempted = False

        def reset_adapter() -> None:
            try:
                pipe.unload_lora_weights()
                active = pipe.get_active_adapters() if hasattr(pipe, "get_active_adapters") else []
                if active:
                    raise RuntimeError("adapter remains active")
            except Exception as exc:
                raise CleanupFailed("adapter reset failed") from exc

        try:
            # Never let a previous request's adapter affect this request.
            reset_adapter()
            if lora:
                adapter_attempted = True
                try:
                    pipe.load_lora_weights(str(lora.path.parent), weight_name=lora.path.name,
                                           adapter_name="request")
                    pipe.set_adapters(["request"], adapter_weights=[request.loras[0].scale])
                    if not hasattr(pipe, "get_active_adapters") or "request" not in pipe.get_active_adapters():
                        raise RuntimeError("LoRA adapter was not activated")
                except Exception as exc:
                    raise LoraIncompatible("LoRA weights could not be applied") from exc
            width, height = (int(part) for part in request.size.split("x"))
            with torch.inference_mode():
                image = pipe(prompt=request.prompt,
                             negative_prompt=request.negative_prompt,
                             width=width, height=height,
                             num_inference_steps=request.num_inference_steps,
                             guidance_scale=request.guidance_scale,
                             generator=torch.Generator(device=self.device).manual_seed(seed)).images[0]
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
        finally:
            if adapter_attempted:
                reset_adapter()

    def unload(self) -> None:
        self.pipeline = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
