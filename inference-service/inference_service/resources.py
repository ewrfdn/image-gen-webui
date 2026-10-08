"""Resource identities shared by discovery, engine and backends."""

from dataclasses import dataclass
from pathlib import Path

QWEN_COMFY_MODEL_ID = "qwen-image-2.1-comfy"
QWEN_COMFY_FILES = {
    "transformer": Path("diffusion_models/qwen_image_2.1_bf16.safetensors"),
    "text_encoder": Path("text_encoders/qwen3vl_8b_bf16.safetensors"),
    "vae": Path("vae/qwen_image_2.1_vae_bf16.safetensors"),
}


@dataclass(frozen=True)
class Checkpoint:
    model_id: str
    path: Path
    resource_dir: Path
    architecture: str
    fingerprint: str
    layout: str = "nested"
    availability: str = "available"
    reason: str | None = None

    def public(self) -> dict:
        result = {"model_id": self.model_id, "architecture": self.architecture,
                  "format": "comfyui" if self.layout == "comfy" else "diffusers",
                  "layout": self.layout, "availability": self.availability,
                  "validation": "structure_checked", "fingerprint": self.fingerprint}
        if self.reason:
            result["reason"] = self.reason
        return result


@dataclass(frozen=True)
class Lora:
    model_id: str
    lora_id: str
    path: Path
    fingerprint: str

    def public(self) -> dict:
        return {"model_id": self.model_id, "lora_id": self.lora_id,
                "filename": self.path.name, "availability": "available",
                "validation": "file_checked", "compatibility": "unchecked",
                "fingerprint": self.fingerprint}
