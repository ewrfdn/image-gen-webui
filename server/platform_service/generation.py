"""Model-aware request validation and durable task admission."""
import asyncio
from sqlalchemy import func, select
from .models import MediaFile, Setting, Task, uid
from .cluster import valid_size
from .errors import ApiError
from .storage import write_atomic

WAITING = ("pending", "processing", "loading")

class GenerationService:
    def __init__(self, platform):
        self.platform = platform

    async def submit(self, owner, payload, source="ui"):
        if not self.platform.ready:
            raise ApiError(503, "registry_initializing", "Catalogue is initializing")
        allowed = {
            "model",
            "revision",
            "prompt",
            "size",
            "n",
            "loras",
            "references",
            "image",
            "lora",
            "watermark",
            "seed",
            "num_inference_steps",
            "guidance_scale",
            "negative_prompt",
            "output_format",
            "quality",
            "response_format",
        }
        if set(payload) - allowed:
            raise ApiError(
                400, "unsupported_parameter", "Unsupported request parameter"
            )
        if (
            payload.get("output_format", "png") != "png"
            or payload.get("quality", "auto") != "auto"
        ):
            raise ApiError(
                400,
                "unsupported_parameter",
                "Only PNG and quality=auto are currently supported",
            )
        if payload.get("response_format", "b64_json") != "b64_json":
            raise ApiError(
                400,
                "unsupported_parameter",
                "Only b64_json is supported by Images compatibility",
            )
        n = payload.get("n", 1)
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 8:
            raise ApiError(400, "invalid_parameter", "n must be an integer from 1 to 8")
        if not isinstance(payload.get("negative_prompt", ""), str) or len(payload.get("negative_prompt", "")) > 4000:
            raise ApiError(400, "invalid_parameter", "Invalid negative_prompt")
        if payload.get("watermark", False) is not False:
            raise ApiError(400, "unsupported_parameter", "Watermark is not supported")
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
            raise ApiError(
                400, "invalid_prompt", "Prompt is required, maximum 4000 characters"
            )
        matches = [
            m
            for m in await self.platform.models_with_defaults()
            if m["model_id"] == payload.get("model")
            and (not payload.get("revision") or m["revision"] == payload["revision"])
        ]
        if len(matches) != 1 or matches[0]["status"] == "unavailable":
            raise ApiError(
                409,
                "model_unavailable",
                "Model is unavailable or its revision is ambiguous",
            )
        model = matches[0]
        c = model["configuration"]
        if n > c["generation"].get("max_count", 1):
            raise ApiError(400, "unsupported_parameter", "Image count exceeds model capability")
        body = dict(payload)
        body["size"] = (
            c["generation"]["default_size"]
            if payload.get("size", "auto") == "auto"
            else payload["size"]
        )
        body.setdefault("num_inference_steps", c["generation"]["default_steps"])
        body.setdefault("guidance_scale", c["generation"]["default_guidance_scale"])
        if c["generation"].get("default_seed") is not None:
            body.setdefault("seed", c["generation"]["default_seed"])
        if not valid_size(body["size"], c["generation"]):
            raise ApiError(400, "unsupported_size", "Size is outside model limits")
        if "lora" in payload and "loras" in payload:
            raise ApiError(400, "invalid_lora", "Use lora or loras, not both")
        selected_loras = payload.get("lora", payload.get("loras", []))
        if isinstance(selected_loras, str):
            selected_loras = [selected_loras]
        if isinstance(selected_loras, list):
            selected_loras = [{"lora_id": v} if isinstance(v, str) else v for v in selected_loras]
        if (
            not isinstance(selected_loras, list)
            or (selected_loras and not c["lora"]["supported"])
            or len(selected_loras) > c["lora"]["max_count"]
        ):
            raise ApiError(400, "invalid_lora", "Invalid LoRA selection count")
        loras = []
        for selection in selected_loras:
            if not isinstance(selection, dict) or set(selection) - {"lora_id", "scale"}:
                raise ApiError(400, "invalid_lora", "Invalid LoRA selection")
            available = [
                l
                for l in model.get("loras", [])
                if l["lora_id"] == selection.get("lora_id")
            ]
            if not available or len({l["sha256"] for l in available}) != 1:
                raise ApiError(
                    400,
                    "invalid_lora",
                    "LoRA is unavailable or has conflicting versions",
                )
            scale = selection.get("scale", 1)
            if not isinstance(scale, (int, float)) or not 0 <= scale <= 2:
                raise ApiError(
                    400, "invalid_lora", "LoRA scale must be between 0 and 2"
                )
            if any(l["lora_id"] == selection["lora_id"] for l in loras):
                raise ApiError(400, "invalid_lora", "Duplicate LoRA selection")
            loras.append({**available[0], "scale": scale})
        if "image" in payload and "references" in payload:
            raise ApiError(400, "invalid_reference", "Use image or references, not both")
        refs = payload.get("image", payload.get("references", []))
        if isinstance(refs, str):
            refs = [refs]
        if (
            not isinstance(refs, list)
            or not all(isinstance(v, str) for v in refs)
            or len(set(refs)) != len(refs)
        ):
            raise ApiError(
                400, "invalid_reference", "References must contain unique upload IDs"
            )
        if refs and not c["reference"]["supported"]:
            raise ApiError(400, "invalid_reference", "Model does not support references")
        if not c["reference"]["min_count"] <= len(refs) <= c["reference"]["max_count"]:
            raise ApiError(
                400, "invalid_reference", "Reference count is outside model limits"
            )
        # Validate numerical extensions before queueing, not after GPU dispatch.
        checks = [
            ("seed", 0, 2**32 - 1, int),
            ("num_inference_steps", 1, c["generation"]["steps_max"], int),
            ("guidance_scale", 0, 20, (int, float)),
        ]
        for key, low, high, kind in checks:
            if key in body and (
                isinstance(body[key], bool)
                or not isinstance(body[key], kind)
                or not low <= body[key] <= high
            ):
                raise ApiError(400, "invalid_parameter", f"Invalid {key}")
        reference_files = [await self.platform.assets.resolve(owner, value, c["reference"]) for value in refs]
        async with self.platform.admission_lock, self.platform.db.sessions() as db:
            setting = await db.scalar(
                select(Setting).where(Setting.id == 1).with_for_update()
            )
            count = await db.scalar(
                select(func.count()).select_from(Task).where(Task.status.in_(WAITING))
            )
            if count >= setting.max_count:
                raise ApiError(429, "queue_full", "Waiting queue is full")
            task = Task(
                id=uid(),
                type=c["type"],
                owner=owner,
                source=source,
                model_id=model["model_id"],
                model_revision=model["revision"],
                model_sha256=model["artifact_sha256"],
                loras=loras,
                capabilities=c,
                request_body=body,
                raw_request=dict(payload),
            )
            db.add(task)
            await db.flush()
            for i, file in enumerate(reference_files):
                db.add(MediaFile(owner=owner, role="reference", task_id=task.id, position=i,
                    storage_key=f"references/{task.id}/{i}", mime=file["mime"], size=file["size"], sha256=file["sha256"]))
                await asyncio.to_thread(write_atomic, self.platform.path(f"references/{task.id}/{i}"), file["data"])
            await db.commit()
        return task.id

