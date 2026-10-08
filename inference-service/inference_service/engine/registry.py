"""Read-only resource discovery. No user-provided path is ever opened."""

from pathlib import Path
import hashlib
import json
import re

from ..resources import Checkpoint, Lora

ID_RE = re.compile(r"[A-Za-z0-9._-]+\Z")
_SKIP_SUFFIXES = (".tmp", ".partial")
_SDXL_COMPONENTS = ("unet", "vae", "text_encoder", "text_encoder_2",
                    "tokenizer", "tokenizer_2", "scheduler")
_QWEN_IMAGE_21_COMPONENTS = ("processor", "scheduler", "text_encoder", "transformer", "vae")
_PIPELINES = {"StableDiffusionXLPipeline": "sdxl", "QwenImage21Pipeline": "qwen_image_21"}
_COMPONENTS = {"sdxl": _SDXL_COMPONENTS, "qwen_image_21": _QWEN_IMAGE_21_COMPONENTS}
_WEIGHT_COMPONENTS = {"sdxl": ("unet", "vae", "text_encoder", "text_encoder_2"),
                      "qwen_image_21": ("text_encoder", "transformer", "vae")}
_SHARD_RE = re.compile(r"(.+)-(\d{5})-of-(\d{5})\.safetensors\Z")


def valid_id(value: str) -> bool:
    return bool(ID_RE.fullmatch(value)) and value not in {".", ".."} and not value.startswith(".") and not value.endswith(_SKIP_SUFFIXES)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _manifest_fingerprint(path: Path, root: Path) -> str:
    entries = []
    for item in sorted(path.rglob("*")):
        parts = item.relative_to(path).parts
        if item.is_file() and _inside(item, root) and "loras" not in parts and not any(part.startswith(".") for part in parts):
            stat = item.stat()
            entries.append((str(item.relative_to(path)), stat.st_size, stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def _weights_complete(directory: Path, root: Path) -> bool:
    indexes = list(directory.glob("*.safetensors.index.json"))
    if indexes:
        for index in indexes:
            try:
                if not _inside(index, root):
                    return False
                weight_map = json.loads(index.read_text(encoding="utf-8"))["weight_map"]
                names = set(weight_map.values()) if isinstance(weight_map, dict) else set()
                if not names or not all(isinstance(name, str) and name.endswith(".safetensors")
                                        and "/" not in name and "\\" not in name
                                        and (directory / name).is_file() and _inside(directory / name, root)
                                        for name in names):
                    return False
            except (OSError, ValueError, TypeError, KeyError):
                return False
        return True
    weights = [weight for weight in directory.glob("*.safetensors") if weight.is_file() and _inside(weight, root)]
    if not weights:
        return False
    for weight in weights:
        match = _SHARD_RE.fullmatch(weight.name)
        if match:
            prefix, _, total = match.groups()
            if not 1 <= int(total) <= 1000:
                return False
            if not all((directory / f"{prefix}-{number:05d}-of-{total}.safetensors").is_file()
                       and _inside(directory / f"{prefix}-{number:05d}-of-{total}.safetensors", root)
                       for number in range(1, int(total) + 1)):
                return False
    return True


class Registry:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def checkpoints(self, include_invalid: bool = False) -> dict[str, Checkpoint]:
        result: dict[str, Checkpoint] = {}
        if not self.root.is_dir():
            return result
        for directory in self.root.iterdir():
            if not valid_id(directory.name) or not directory.is_dir() or not _inside(directory, self.root):
                continue
            nested_model = directory / "model"
            model_dir = nested_model if nested_model.is_dir() else directory
            layout = "nested" if model_dir == nested_model else "direct"
            reason = None
            architecture = "unsupported"
            if not model_dir.is_dir() or not _inside(model_dir, self.root):
                reason = "missing_model_directory"
            else:
                try:
                    index_path = model_dir / "model_index.json"
                    if not _inside(index_path, self.root):
                        raise OSError("model index outside resource root")
                    index = json.loads(index_path.read_text(encoding="utf-8"))
                    architecture = _PIPELINES.get(index.get("_class_name"), "unsupported")
                    if architecture == "unsupported":
                        reason = "unsupported_pipeline"
                    else:
                        for component in _COMPONENTS[architecture]:
                            part = model_dir / component
                            if not part.is_dir() or not _inside(part, self.root) or not any(part.iterdir()):
                                reason = "incomplete_component"
                                break
                        if reason is None:
                            for component in _WEIGHT_COMPONENTS[architecture]:
                                if not _weights_complete(model_dir / component, self.root):
                                    reason = "missing_weights"
                                    break
                except (OSError, ValueError, TypeError):
                    reason = "invalid_model_index"
            if reason and not include_invalid:
                continue
            try:
                fingerprint = _manifest_fingerprint(model_dir, self.root) if reason is None else ""
            except OSError:
                reason = "resource_changed"
                fingerprint = ""
            if reason and not include_invalid:
                continue
            result[directory.name] = Checkpoint(directory.name, model_dir, directory, architecture,
                fingerprint, layout, "available" if reason is None else "invalid", reason)
        return result

    def loras(self, model_id: str | None = None) -> list[Lora]:
        models = self.checkpoints()
        if model_id is not None:
            models = {model_id: models[model_id]} if model_id in models else {}
        found: list[Lora] = []
        for checkpoint in models.values():
            directory = checkpoint.resource_dir / "loras"
            if not directory.is_dir() or not _inside(directory, self.root):
                continue
            for path in directory.iterdir():
                if path.suffix != ".safetensors" or not valid_id(path.stem) or not path.is_file() or not _inside(path, self.root):
                    continue
                try:
                    with path.open("rb") as stream:
                        length = int.from_bytes(stream.read(8), "little")
                        if length < 2 or length > 100_000_000 or length > path.stat().st_size - 8:
                            continue
                        header = json.loads(stream.read(length))
                        tensors = [value for key, value in header.items() if key != "__metadata__"] if isinstance(header, dict) else []
                        if not tensors or not all(isinstance(value, dict) and isinstance(value.get("data_offsets"), list)
                              and len(value["data_offsets"]) == 2 and 0 <= value["data_offsets"][0] < value["data_offsets"][1]
                              <= path.stat().st_size - 8 - length for value in tensors):
                            continue
                    stat = path.stat()
                    fingerprint = f"{stat.st_size}:{stat.st_mtime_ns}"
                    found.append(Lora(checkpoint.model_id, path.stem, path, fingerprint))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
        return sorted(found, key=lambda item: (item.model_id, item.lora_id))

    def lora(self, model_id: str, lora_id: str) -> Lora | None:
        if not valid_id(model_id) or not valid_id(lora_id):
            return None
        return next((item for item in self.loras(model_id) if item.lora_id == lora_id), None)
