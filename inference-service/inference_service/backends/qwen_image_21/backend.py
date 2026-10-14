"""Local text-to-image inference for the Qwen-Image 2.1 Diffusers pipeline."""

import gc
import base64
import io
from typing import Any

from ...resources import Checkpoint, Lora
from ...schemas import GenerationRequest
from ..base import CleanupFailed, LoraIncompatible, UnsupportedParameter


def validated_lora_state(pipe, lora: Lora) -> dict:
    state = pipe.lora_state_dict(str(lora.path.parent), weight_name=lora.path.name,
                                 use_safetensors=True, local_files_only=True)
    pairs = {}
    for key, tensor in state.items():
        if not key.startswith("transformer."):
            raise ValueError(f"Unsupported LoRA component: {key}")
        name = key.removeprefix("transformer.")
        kind = next((kind for kind in ("A", "B") if name.endswith(f".lora_{kind}.weight")), None)
        if kind is None or len(tensor.shape) != 2:
            raise ValueError(f"Unsupported LoRA tensor: {key}")
        module_name = name.removesuffix(f".lora_{kind}.weight")
        module = pipe.transformer.get_submodule(module_name)
        if not hasattr(module, "weight") or len(module.weight.shape) != 2:
            raise ValueError(f"Unsupported LoRA module: {module_name}")
        pairs.setdefault(module_name, {})[kind] = tensor
    if not pairs:
        raise ValueError("LoRA contains no transformer adapters")
    for name, pair in pairs.items():
        if set(pair) != {"A", "B"}:
            raise ValueError(f"Incomplete LoRA pair: {name}")
        out_features, in_features = pipe.transformer.get_submodule(name).weight.shape
        if pair["A"].shape[1] != in_features or pair["B"].shape[0] != out_features or pair["A"].shape[0] != pair["B"].shape[1]:
            raise ValueError(f"Incompatible LoRA dimensions: {name}")
    return state


class QwenImage21Backend:
    def __init__(self, device: str, dtype: str, model_offload: str = "none"):
        self.device = device
        self.dtype = dtype
        self.model_offload = model_offload
        self.pipeline: Any = None

    def load(self, checkpoint: Checkpoint) -> None:
        import torch
        from diffusers import QwenImage21Pipeline

        dtype = {"float16": torch.float16, "bfloat16": torch.bfloat16,
                 "float32": torch.float32}[self.dtype]
        pipe = QwenImage21Pipeline.from_pretrained(
            str(checkpoint.path), dtype=dtype, use_safetensors=True,
            local_files_only=True)
        # Keep a reference before GPU transfer so a failed transfer can be unloaded.
        self.pipeline = pipe
        if self.model_offload == "model_cpu":
            pipe.enable_model_cpu_offload(gpu_id=int(self.device.split(":", 1)[1]))
        elif self.model_offload == "sequential_cpu":
            pipe.enable_sequential_cpu_offload(gpu_id=int(self.device.split(":", 1)[1]))
        else:
            self.pipeline = pipe.to(self.device)
        with torch.inference_mode():
            self.pipeline(prompt="warmup", width=512, height=512,
                          num_inference_steps=1, true_cfg_scale=1.0,
                          generator=torch.Generator(device=self.device).manual_seed(0))

    def generate(self, request: GenerationRequest, lora: Lora | list[Lora] | None, seed: int) -> bytes:
        import torch

        pipe = self.pipeline
        if pipe is None:
            raise RuntimeError("pipeline is not loaded")
        references = []
        try:
            from PIL import Image
            for encoded in request.image:
                data = base64.b64decode(encoded, validate=True)
                if len(data) > 20 * 1024 * 1024:
                    raise ValueError("Reference exceeds 20 MB")
                with Image.open(io.BytesIO(data)) as image:
                    if image.format not in ("PNG", "JPEG", "WEBP"):
                        raise ValueError("Unsupported reference format")
                    references.append(image.convert("RGBA"))
        except Exception as exc:
            raise UnsupportedParameter("Reference must be a valid PNG, JPEG or WebP, at most 20 MB") from exc
        guidance = request.guidance_scale if request.guidance_scale is not None else 1.0
        if request.negative_prompt and guidance <= 1:
            raise UnsupportedParameter("negative_prompt requires guidance_scale > 1 for Qwen-Image 2.1")

        def reset_adapter() -> None:
            try:
                pipe.unload_lora_weights()
                active = pipe.get_active_adapters() if hasattr(pipe, "get_active_adapters") else []
                if active:
                    raise RuntimeError("adapter remains active")
            except Exception as exc:
                raise CleanupFailed("adapter reset failed") from exc

        adapter_attempted = False
        try:
            reset_adapter()
            if lora:
                adapter_attempted = True
                try:
                    selected = lora if isinstance(lora, list) else [lora]
                    names = []
                    for index, item in enumerate(selected):
                        name = "request" if len(selected) == 1 else f"request_{index}"
                        state = validated_lora_state(pipe, item)
                        pipe.load_lora_weights(state, adapter_name=name)
                        names.append(name)
                    pipe.set_adapters(names, adapter_weights=[v.scale for v in request.loras])
                    if not hasattr(pipe, "get_active_adapters") or not set(names).issubset(pipe.get_active_adapters()):
                        raise RuntimeError("LoRA adapter was not activated")
                except Exception as exc:
                    raise LoraIncompatible("LoRA weights could not be applied") from exc
            width, height = (int(part) for part in request.size.split("x"))
            with torch.inference_mode():
                image = pipe(**({"image": references} if references else {}), prompt=request.prompt,
                             negative_prompt=(request.negative_prompt or "") if guidance > 1 else None,
                             true_cfg_scale=guidance,
                             width=width, height=height,
                             num_inference_steps=request.num_inference_steps or 40,
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
