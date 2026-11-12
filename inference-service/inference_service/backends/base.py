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


class BackendUnavailable(Exception):
    """The execution backend is down or temporarily cannot accept work.

    Unlike a failed generation this is recoverable: the model slot stays ready
    and the caller should retry after the advertised delay.
    """


@dataclass(frozen=True)
class BackendCapabilities:
    architecture: str
    min_size: int
    max_size: int
    multiple_of: int
    max_pixels: int
    default_steps: int
    default_guidance: float
    reference_max_count: int = 0
    # Distilled models sample on a fixed grid and bake guidance into the weights.
    # Advertise that instead of publishing a step range the backend would reject.
    fixed_steps: int | None = None
    default_size: str = "1024x1024"
    supports_guidance: bool = True
    supports_negative_prompt: bool = True
    lora_limit: int = 8
    max_count: int = 8

    def validate_size(self, size: str) -> bool:
        width, height = (int(part) for part in size.split("x"))
        return (self.min_size <= width <= self.max_size
                and self.min_size <= height <= self.max_size
                and width * height <= self.max_pixels
                and width % self.multiple_of == 0
                and height % self.multiple_of == 0)

    def public(self) -> dict:
        steps = ({"min": self.fixed_steps, "max": self.fixed_steps, "default": self.fixed_steps,
                  "fixed": True}
                 if self.fixed_steps is not None
                 else {"min": 1, "max": 100, "default": self.default_steps, "fixed": False})
        return {"architecture": self.architecture,
                "sizes": {"min": self.min_size, "max": self.max_size,
                          "multiple_of": self.multiple_of, "max_pixels": self.max_pixels},
                "steps": steps,
                "guidance_scale": {"default": self.default_guidance,
                                   "supported": self.supports_guidance},
                "negative_prompt": {"supported": self.supports_negative_prompt},
                "lora_limit": self.lora_limit, "max_count": self.max_count,
                "native_batch": False, "reference_max_count": self.reference_max_count}


class Backend(Protocol):
    def load(self, checkpoint: Checkpoint) -> None: ...

    def generate(self, request: GenerationRequest, lora: Lora | list[Lora] | None, seed: int) -> bytes: ...

    def unload(self) -> None: ...
