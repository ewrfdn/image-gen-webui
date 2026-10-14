"""Offline deployment manifest builder. Does not move weights or load a GPU model."""

import argparse
import hashlib
import json
from pathlib import Path
from inference_service.engine.registry import Registry
from inference_service.backends.factory import CAPABILITIES


def build(root):
    for checkpoint in Registry(root).checkpoints().values():
        manifest_path = checkpoint.resource_dir / "artifact-manifest.json"
        files = []
        old = {}
        if manifest_path.exists():
            old = {f["path"]: f for f in json.loads(manifest_path.read_text())["files"]}
        for file in sorted(checkpoint.path.rglob("*")):
            relative = file.relative_to(checkpoint.path)
            if (
                not file.is_file()
                or "loras" in relative.parts
                or any(p.startswith(".") for p in relative.parts)
                or file.name in ("model-config.json", "artifact-manifest.json")
            ):
                continue
            stat = file.stat()
            previous = old.get(relative.as_posix())
            if (
                previous
                and previous["size"] == stat.st_size
                and previous.get("mtime_ns") == stat.st_mtime_ns
            ):
                digest = previous["sha256"]
            else:
                hasher = hashlib.sha256()
                with file.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                        hasher.update(chunk)
                digest = hasher.hexdigest()
            files.append(
                {
                    "path": relative.as_posix(),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": digest,
                }
            )
        content = [{k: v for k, v in f.items() if k != "mtime_ns"} for f in files]
        digest = hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        manifest_path.write_text(
            json.dumps({"artifact_sha256": digest, "files": files}, indent=2)
        )
        caps = CAPABILITIES[checkpoint.architecture]
        config_path = checkpoint.resource_dir / "model-config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text())
            config.update(revision=digest, artifact_sha256=digest)
        else:
            config = {
                "schema_version": 1,
                "model_id": checkpoint.model_id,
                "display_name": checkpoint.model_id,
                "revision": digest,
                "artifact_sha256": digest,
                "type": "image",
                "architecture": checkpoint.architecture,
                "reference": {
                    "supported": bool(caps.reference_max_count),
                    "mime_types": ["image/png", "image/jpeg", "image/webp"] if caps.reference_max_count else [],
                    "min_count": 0,
                    "max_count": caps.reference_max_count,
                    "max_file_bytes": 20 * 1024 * 1024 if caps.reference_max_count else 0,
                },
                "lora": {"supported": True, "max_count": 8},
                "generation": {
                    "max_count": 8,
                    "default_size": "1024x1024",
                    "width_min": caps.min_size,
                    "width_max": caps.max_size,
                    "height_min": caps.min_size,
                    "height_max": caps.max_size,
                    "dimension_multiple": caps.multiple_of,
                    "max_pixels": caps.max_pixels,
                    "default_steps": caps.default_steps,
                    "steps_max": 100,
                    "default_guidance_scale": caps.default_guidance,
                    "output_mime_types": ["image/png"],
                    "timeout_seconds": 900,
                },
            }
        config_path.write_text(json.dumps(config, indent=2))
        print(checkpoint.model_id, digest[:16], len(files), "files")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    build(parser.parse_args().root)
