"""Prepare isolated DGX model mounts and credentials without altering original weights.

Run from inference-service on DGX: python3 scripts/prepare_dgx_dev.py
"""
import argparse
import json
from pathlib import Path
import secrets
import shutil
import struct
import hashlib


def convert_qwen_lora(source: Path, target: Path) -> bool:
    """Losslessly split AI-toolkit's [gate; up] LoRA into Diffusers projections."""
    raw = source.read_bytes()
    length = struct.unpack("<Q", raw[:8])[0]
    if length > 16 * 1024 * 1024:
        raise ValueError("Unexpected safetensors header")
    header = json.loads(raw[8:8 + length])
    fused = [key for key in header if key.endswith(".img_mlp.gate_up.lora_A.weight")]
    if not fused:
        return False
    source_hash = hashlib.sha256(raw).hexdigest()
    if target.exists():
        with target.open("rb") as stream:
            existing_length = struct.unpack("<Q", stream.read(8))[0]
            if existing_length <= 16 * 1024 * 1024:
                existing = json.loads(stream.read(existing_length))
                if existing.get("__metadata__", {}).get("inference_source_sha256") == source_hash:
                    return True
    payload = memoryview(raw)[8 + length:]
    entries = {key: (value, payload[value["data_offsets"][0]:value["data_offsets"][1]])
               for key, value in header.items() if key != "__metadata__"}
    for key in fused:
        prefix = key.removesuffix("lora_A.weight")
        a, a_bytes = entries.pop(key)
        b, b_bytes = entries.pop(prefix + "lora_B.weight")
        if len(a["shape"]) != 2 or len(b["shape"]) != 2 or b["shape"][0] % 2 or b["shape"][1] != a["shape"][0]:
            raise ValueError("Invalid fused LoRA dimensions")
        for index, projection in enumerate(("gate_layer", "proj")):
            new_prefix = prefix.replace(".img_mlp.gate_up.", f".img_mlp.{projection}.")
            if new_prefix + "lora_A.weight" in entries or new_prefix + "lora_B.weight" in entries:
                raise ValueError("Both fused and split LoRA projections are present")
            entries[new_prefix + "lora_A.weight"] = (a, a_bytes)
            half = len(b_bytes) // 2
            entries[new_prefix + "lora_B.weight"] = (
                {**b, "shape": [b["shape"][0] // 2, b["shape"][1]]}, b_bytes[index * half:(index + 1) * half])
        alpha = entries.pop(prefix + "alpha", None)
        if alpha:
            for projection in ("gate_layer", "proj"):
                entries[prefix.replace(".img_mlp.gate_up.", f".img_mlp.{projection}.") + "alpha"] = alpha
    metadata = {**header.get("__metadata__", {}), "inference_source_sha256": source_hash,
                "inference_conversion": "qwen21_gate_up_to_gate_layer_proj_v1"}
    converted = {"__metadata__": metadata}
    offset = 0
    for key, (tensor, data) in entries.items():
        converted[key] = {**tensor, "data_offsets": [offset, offset + len(data)]}
        offset += len(data)
    encoded = json.dumps(converted, separators=(",", ":")).encode()
    encoded += b" " * (-len(encoded) % 8)
    temporary = target.with_name("." + target.name + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(struct.pack("<Q", len(encoded)))
            stream.write(encoded)
            for _, data in entries.values():
                stream.write(data)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path.home() / "models")
    args = parser.parse_args()
    service = Path(__file__).resolve().parents[1]
    (service / "verification-output").mkdir(exist_ok=True)
    root = service.parent / "models"
    source = args.source.expanduser().resolve()
    names = ["noobai-XL-1.1", "qwen-image-2.1"]
    for name in names:
        if not (source / name / "model_index.json").is_file():
            raise RuntimeError(f"Missing Diffusers checkpoint: {source / name}")
        (root / name / "model").mkdir(parents=True, exist_ok=True)
        (root / name / "loras").mkdir(exist_ok=True)
    candidates = [source / "bocchi_illustrious_lora.safetensors", *sorted((source / "loras").glob("bocchi_lora_v*.safetensors"))]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        with candidate.open("rb") as stream:
            length = struct.unpack("<Q", stream.read(8))[0]
            if length > 16 * 1024 * 1024:
                raise RuntimeError(f"Unexpected safetensors header: {candidate}")
            header = json.loads(stream.read(length))
        metadata = header.get("__metadata__", {})
        if metadata.get("ss_base_model_version") == "qwen_image_2":
            model = "qwen-image-2.1"
        elif candidate.name == "bocchi_illustrious_lora.safetensors" and any(k.startswith("lora_te1_") for k in header):
            model = "noobai-XL-1.1"
        else:
            print(f"Skip unrecognized adapter: {candidate.name}")
            continue
        target = root / model / "loras" / candidate.name
        converted = model == "qwen-image-2.1" and convert_qwen_lora(candidate, target)
        if not converted and not target.exists():
            shutil.copy2(candidate, target)
        print(f"{model}: {candidate.name}; fused MLP converted={converted}; GPU acceptance required")
    override = {"services": {"inference": {"volumes": [
        {"type": "bind", "source": str(source / name), "target": f"/models/{name}/model", "read_only": True}
        for name in names]}}}
    (service / "compose.resources.json").write_text(json.dumps(override, indent=2), encoding="utf-8")
    env = service / ".env"
    if not env.exists():
        env.write_text(f"SERVICE_VERSION=0.1.0\nHOST_MODEL_ROOT={root}\nINFERENCE_PORT=18001\n"
                       f"INFERENCE_TOKEN={secrets.token_urlsafe(32)}\nADMIN_TOKEN={secrets.token_urlsafe(32)}\n"
                       "MODEL_OFFLOAD=none\n", encoding="utf-8")
        env.chmod(0o600)
    print("Prepared .env and compose.resources.json; existing credentials preserved")


if __name__ == "__main__":
    main()
