"""The interface and failure types shared by model backends."""

from dataclasses import dataclass
from typing import Protocol

from ..resources import Checkpoint, Lora
from ..schemas import GenerationRequest


class CleanupFailed(Exception):
    """A backend cannot guarantee that request state was reset."""


class LoraIncompatible(Exception):
    """The requested LoRA cannot be applied to the loaded model."""


class UnsupportedParameter(Exception):
    """A request parameter cannot be honored by this architecture."""


@dataclass(frozen=True)
class BackendCapabilities:
    architecture: str
    min_size: int
    max_size: int
    multiple_of: int
    max_pixels: int
    default_steps: int
    default_guidance: float
    lora_limit: int = 1

    def validate_size(self, size: str) -> bool:
        width, height = (int(part) for part in size.split("x"))
        return (self.min_size <= width <= self.max_size
                and self.min_size <= height <= self.max_size
                and width * height <= self.max_pixels
                and width % self.multiple_of == 0
                and height % self.multiple_of == 0)

    def public(self) -> dict:
        return {"architecture": self.architecture,
                "sizes": {"min": self.min_size, "max": self.max_size,
                          "multiple_of": self.multiple_of, "max_pixels": self.max_pixels},
                "steps": {"min": 1, "max": 100, "default": self.default_steps},
                "guidance_scale": {"default": self.default_guidance},
                "lora_limit": self.lora_limit}


class Backend(Protocol):
    def load(self, checkpoint: Checkpoint) -> None: ...

    def generate(self, request: GenerationRequest, lora: Lora | None, seed: int) -> bytes: ...

    def unload(self) -> None: ...
