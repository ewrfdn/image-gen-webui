import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import logging
import secrets
import threading
import time
import uuid
from typing import Callable

import psutil

from ..backends.base import Backend, CleanupFailed, LoraIncompatible, UnsupportedParameter
from ..backends.factory import CAPABILITIES, create_backend
from ..config import Settings
from ..resources import Checkpoint
from ..schemas import GenerationRequest
from .registry import Registry, valid_id

LOGGER = logging.getLogger(__name__)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class Engine:
    def __init__(self, settings: Settings,
                 backend_factory: Callable[[Checkpoint], Backend] | None = None):
        self.settings = settings
        self.registry = Registry(settings.model_root)
        self.backend_factory = backend_factory or (lambda checkpoint: create_backend(checkpoint, settings))
        self.boot_id = str(uuid.uuid4())
        self.operation_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.backend: Backend | None = None
        self.model_id: str | None = None
        self.fingerprint: str | None = None
        self.state = "unloaded"
        self.loaded_at: str | None = None
        self.active_requests = 0
        self.last_error: str | None = None

    def acquire(self) -> None:
        if not self.operation_lock.acquire(blocking=False):
            raise ServiceError(503, "engine_busy", "Engine is processing another operation", 1)

    def release(self) -> None:
        self.operation_lock.release()

    async def load(self, model_id: str) -> dict:
        self.acquire()
        try:
            return await asyncio.to_thread(self.load_locked, model_id)
        finally:
            self.release()

    async def unload(self, model_id: str) -> dict:
        if self.active_requests and self.model_id == model_id:
            raise ServiceError(409, "model_in_use", "Model is in use")
        self.acquire()
        try:
            return await asyncio.to_thread(self.unload_locked, model_id)
        finally:
            self.release()

    async def generate(self, request: GenerationRequest) -> dict:
        self.acquire()
        try:
            return await asyncio.to_thread(self.generate_locked, request)
        finally:
            self.release()

    def snapshot(self, loaded_only: bool = False) -> dict:
        models = self.registry.checkpoints()
        with self.state_lock:
            current = (self.model_id, self.fingerprint, self.state, self.active_requests,
                       self.loaded_at, self.last_error, self.backend is not None)
        current_id, fingerprint, state, active, loaded_at, last_error, resident = current
        result = []
        if not loaded_only:
            for item in models.values():
                if item.model_id != current_id:
                    result.append({"model_id": item.model_id, "revision": item.fingerprint,
                                   "state": "unloaded", "active_requests": 0,
                                   "can_generate": False, "source_status": "present"})
        if current_id and (not loaded_only or resident):
            source = models.get(current_id)
            source_status = "missing" if source is None else ("present" if source.fingerprint == fingerprint else "changed")
            result.append({"model_id": current_id, "revision": fingerprint, "state": state,
                           "device": self.settings.device,
                           "placement": "cpu_offload" if self.settings.model_offload != "none" else ("gpu" if "cuda" in self.settings.device else "cpu"),
                           "active_requests": active, "can_generate": state == "ready" and source_status == "present",
                           "source_status": source_status, "loaded_at": loaded_at,
                           "last_error": last_error})
        return {"instance_id": self.settings.instance_id, "boot_id": self.boot_id,
                "max_loaded_models": 1, "models": result}

    def resources(self) -> dict:
        memory = psutil.virtual_memory()
        result = {"sampled_at": int(time.time()), "host_memory": {
            "total_bytes": memory.total, "available_bytes": memory.available}, "gpu": None}
        try:
            import torch
            if torch.cuda.is_available():
                free, total = torch.cuda.mem_get_info(self.settings.device)
                result["gpu"] = {"device": self.settings.device,
                                 "name": torch.cuda.get_device_name(self.settings.device),
                                 "total_bytes": total, "free_bytes": free,
                                 "process_allocated_bytes": torch.cuda.memory_allocated(self.settings.device),
                                 "process_reserved_bytes": torch.cuda.memory_reserved(self.settings.device)}
        except (ImportError, RuntimeError, AssertionError):
            pass
        return result

    def capabilities(self) -> dict:
        return {"instance_id": self.settings.instance_id, "max_loaded_models": 1,
                "model_offload": self.settings.model_offload,
                "busy": self.operation_lock.locked(),
                "models": [{"model_id": item.model_id, **CAPABILITIES[item.architecture].public(),
                    "loras": [lora.public() for lora in self.registry.loras(item.model_id)]}
                    for item in self.registry.checkpoints().values()]}

    def load_locked(self, model_id: str) -> dict:
        if not valid_id(model_id):
            raise ServiceError(404, "model_not_found", "Model not found")
        models = self.registry.checkpoints()
        checkpoint = models.get(model_id)
        if checkpoint is None:
            raise ServiceError(404, "model_not_found", "Model not found")
        with self.state_lock:
            if self.state in ("loading", "unloading"):
                raise ServiceError(409, "model_transitioning", "Model is transitioning")
            if self.model_id == model_id and self.state == "ready":
                if checkpoint.fingerprint != self.fingerprint:
                    raise ServiceError(409, "resource_changed", "Model files changed; unload first")
                return {"model_id": model_id, "state": "ready", "revision": self.fingerprint}
            if self.backend is not None:
                raise ServiceError(409, "capacity_conflict", f"Model slot occupied by {self.model_id}")
            self.state = "loading"
            self.model_id = model_id
            self.last_error = None
        backend = None
        try:
            backend = self.backend_factory(checkpoint)
            backend.load(checkpoint)
            refreshed = self.registry.checkpoints().get(model_id)
            if refreshed is None or refreshed.fingerprint != checkpoint.fingerprint:
                raise ServiceError(409, "resource_changed", "Model files changed during load")
        except Exception as exc:
            LOGGER.exception("Model load failed: model_id=%s", model_id)
            try:
                if backend is not None:
                    backend.unload()
            except Exception:
                with self.state_lock:
                    self.backend = backend
                    self.state = "error"
                    self.last_error = "load_cleanup_failed"
                raise ServiceError(503, "engine_recovering", "Model cleanup failed") from exc
            with self.state_lock:
                self.model_id = None
                self.state = "unloaded"
                self.last_error = "load_failed"
            if isinstance(exc, ServiceError):
                raise
            if "out of memory" in str(exc).lower():
                raise ServiceError(500, "gpu_oom", "GPU ran out of memory during model load") from exc
            raise ServiceError(500, "load_failed", "Model load or warmup failed") from exc
        with self.state_lock:
            self.backend = backend
            self.fingerprint = checkpoint.fingerprint
            self.state = "ready"
            self.loaded_at = datetime.now(timezone.utc).isoformat()
        return {"model_id": model_id, "state": "ready", "revision": checkpoint.fingerprint}

    def unload_locked(self, model_id: str) -> dict:
        if not valid_id(model_id):
            raise ServiceError(404, "model_not_found", "Model not found")
        with self.state_lock:
            if self.model_id != model_id or self.backend is None:
                return {"model_id": model_id, "state": "unloaded"}
            if self.active_requests:
                raise ServiceError(409, "model_in_use", "Model is in use")
            self.state = "unloading"
            backend = self.backend
        try:
            backend.unload()
        except Exception as exc:
            LOGGER.exception("Model unload failed: model_id=%s", model_id)
            with self.state_lock:
                self.state = "error"
                self.last_error = "unload_failed"
            raise ServiceError(503, "engine_recovering", "Model cleanup failed") from exc
        with self.state_lock:
            self.backend = None
            self.model_id = None
            self.fingerprint = None
            self.loaded_at = None
            self.state = "unloaded"
        return {"model_id": model_id, "state": "unloaded"}

    def generate_locked(self, request: GenerationRequest) -> dict:
        if not valid_id(request.model):
            raise ServiceError(404, "model_not_found", "Model not found")
        checkpoint = self.registry.checkpoints().get(request.model)
        with self.state_lock:
            if self.state == "error":
                raise ServiceError(503, "engine_recovering", "Engine requires recovery", 1)
            if self.model_id != request.model or self.backend is None:
                if checkpoint is None:
                    raise ServiceError(404, "model_not_found", "Model not found")
                raise ServiceError(409, "model_not_loaded", "Model is not loaded")
            if self.state != "ready":
                raise ServiceError(409, "model_transitioning", "Model is transitioning")
            if checkpoint is None or checkpoint.fingerprint != self.fingerprint:
                raise ServiceError(409, "resource_changed", "Model files changed; unload and reload")
            if not CAPABILITIES[checkpoint.architecture].validate_size(request.size):
                raise ServiceError(400, "unsupported_size", "Image size is not supported by this model")
            backend = self.backend
            revision = self.fingerprint
            self.active_requests = 1
        seed = request.seed if request.seed is not None else secrets.randbelow(2**32)
        start = time.perf_counter()
        try:
            lora = None
            lora_info = []
            if request.loras:
                lora = self.registry.lora(request.model, request.loras[0].lora_id)
                if lora is None:
                    raise ServiceError(404, "lora_not_found", "LoRA not found")
                hasher = hashlib.sha256()
                with lora.path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                if self.registry.lora(request.model, lora.lora_id) != lora:
                    raise ServiceError(409, "resource_changed", "LoRA file changed")
                lora_info = [{"lora_id": lora.lora_id, "scale": request.loras[0].scale,
                              "sha256": hasher.hexdigest()}]
            png = backend.generate(request, lora, seed)
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("backend did not produce PNG")
            if lora and self.registry.lora(request.model, lora.lora_id) != lora:
                raise ServiceError(409, "resource_changed", "LoRA file changed during generation")
            return {"created": int(time.time()), "data": [{"b64_json": base64.b64encode(png).decode("ascii")}],
                    "metadata": {"model_revision": revision, "seed": seed,
                                 "loras": lora_info, "inference_ms": int((time.perf_counter() - start) * 1000)}}
        except CleanupFailed as exc:
            LOGGER.exception("Adapter cleanup failed: model_id=%s", request.model)
            with self.state_lock:
                self.state = "error"
                self.last_error = "adapter_cleanup_failed"
            raise ServiceError(500, "adapter_cleanup_failed", "Adapter cleanup failed") from exc
        except LoraIncompatible as exc:
            LOGGER.warning("LoRA rejected: model_id=%s lora_id=%s", request.model,
                           request.loras[0].lora_id if request.loras else "")
            raise ServiceError(400, "lora_incompatible", "LoRA cannot be applied to this model") from exc
        except UnsupportedParameter as exc:
            raise ServiceError(400, "unsupported_parameter", str(exc)) from exc
        except ServiceError:
            raise
        except Exception as exc:
            LOGGER.exception("Generation failed: model_id=%s", request.model)
            if "out of memory" in str(exc).lower():
                with self.state_lock:
                    self.state = "error"
                    self.last_error = "gpu_oom"
                raise ServiceError(500, "gpu_oom", "GPU ran out of memory") from exc
            raise ServiceError(500, "generation_failed", "Generation failed") from exc
        finally:
            with self.state_lock:
                self.active_requests = 0
