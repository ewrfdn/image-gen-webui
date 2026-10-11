"""Read-only model JSON beside checkpoint directories; no business catalogue state."""

import hashlib
import json
import re
from pathlib import Path
from ..backends.factory import CAPABILITIES

_LORA_DIGESTS = {}


def content_digest(path):
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _LORA_DIGESTS:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        _LORA_DIGESTS[key] = digest.hexdigest()
    return _LORA_DIGESTS[key]


def configuration(checkpoint):
    path = checkpoint.resource_dir / "model-config.json"
    if not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("model_config_missing")
    c = json.loads(path.read_text(encoding="utf-8"))
    caps = CAPABILITIES[checkpoint.architecture]
    if (
        c["schema_version"] != 1
        or c["model_id"] != checkpoint.model_id
        or c["architecture"] != checkpoint.architecture
        or c["type"] != "image"
    ):
        raise ValueError("model_config_identity_mismatch")
    if (
        not c["revision"]
        or not re.fullmatch("[a-f0-9]{64}", c["artifact_sha256"])
        or c["artifact_sha256"] == "0" * 64
    ):
        raise ValueError("model_config_revision_invalid")
    # This infra currently implements text-to-image only. Declarations cannot enable edits/video.
    r = c["reference"]
    if r != {
        "supported": False,
        "mime_types": [],
        "min_count": 0,
        "max_count": 0,
        "max_file_bytes": 0,
    }:
        raise ValueError("reference_backend_not_implemented")
    g = c["generation"]
    if (
        g["width_min"] < caps.min_size
        or g["height_min"] < caps.min_size
        or g["width_max"] > caps.max_size
        or g["height_max"] > caps.max_size
        or g["max_pixels"] > caps.max_pixels
        or g["dimension_multiple"] <= 0
        or g["dimension_multiple"] % caps.multiple_of
        or not 1 <= g["default_steps"] <= g["steps_max"] <= 100
        or g["output_mime_types"] != ["image/png"]
        or g["timeout_seconds"] < 1
        or not 0 <= g["default_guidance_scale"] <= 20
    ):
        raise ValueError("model_config_exceeds_backend_capabilities")
    w, h = map(int, g["default_size"].split("x"))
    if (
        not g["width_min"] <= w <= g["width_max"]
        or not g["height_min"] <= h <= g["height_max"]
        or w * h > g["max_pixels"]
        or w % g["dimension_multiple"]
        or h % g["dimension_multiple"]
    ):
        raise ValueError("model_config_default_size_invalid")
    if (
        c["lora"]["max_count"] not in (0, 1)
        or bool(c["lora"]["max_count"]) != c["lora"]["supported"]
    ):
        raise ValueError("model_config_lora_invalid")
    manifest_path = checkpoint.resource_dir / "artifact-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["artifact_sha256"] != c["artifact_sha256"]:
        raise ValueError("manifest_identity_mismatch")
    # Deployment calculates file content hashes. Per-query validate names and sizes without rereading huge weights.
    for item in manifest["files"]:
        target = (checkpoint.path / item["path"]).resolve()
        if (
            not target.is_relative_to(checkpoint.path.resolve())
            or not target.is_file()
            or target.stat().st_size != item["size"]
            or target.stat().st_mtime_ns != item.get("mtime_ns")
        ):
            raise ValueError("manifest_resource_changed")
    return c


def catalogue(engine):
    models = []
    errors = []
    for checkpoint in engine.registry.checkpoints().values():
        try:
            c = configuration(checkpoint)
            loras = []
            if c["lora"]["supported"]:
                for lora in engine.registry.loras(checkpoint.model_id):
                    digest = content_digest(lora.path)
                    loras.append(
                        {
                            "lora_id": lora.lora_id,
                            "revision": digest,
                            "sha256": digest,
                            "display_name": lora.lora_id,
                            "compatibility": "checked_during_generation",
                        }
                    )
            models.append(
                {
                    "model_id": checkpoint.model_id,
                    "revision": c["revision"],
                    "artifact_sha256": c["artifact_sha256"],
                    "configuration": c,
                    "local_available": True,
                    "loras": loras,
                }
            )
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append({"model_id": checkpoint.model_id, "reason": str(exc)[:160]})
    encoded = json.dumps(models, sort_keys=True, separators=(",", ":")).encode()
    runtime = engine.snapshot()
    runtime["busy"] = engine.operation_lock.locked()
    return {
        "catalog_revision": hashlib.sha256(encoded).hexdigest(),
        "models": models,
        "errors": errors,
        "runtime": runtime,
    }
