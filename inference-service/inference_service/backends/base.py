"""The interface and failure types shared by model backends."""

from typing import Protocol

from ..resources import Checkpoint, Lora
from ..schemas import GenerationRequest


class CleanupFailed(Exception):
    """A backend cannot guarantee that request state was reset."""


class LoraIncompatible(Exception):
    """The requested LoRA cannot be applied to the loaded model."""


class Backend(Protocol):
    def load(self, checkpoint: Checkpoint) -> None: ...

    def generate(self, request: GenerationRequest, lora: Lora | None, seed: int) -> bytes: ...

    def unload(self) -> None: ...
