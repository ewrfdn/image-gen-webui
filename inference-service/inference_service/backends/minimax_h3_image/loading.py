"""Shard loaders that keep a 62 GB component off the slow copy path.

Transformers 5.x and Diffusers both stream weights straight out of the safetensors
mapping. On this machine that copy path degrades to a few MB/s once the page cache is
under pressure, so a 63 GB text encoder takes hours instead of seconds. Staging each
tensor through clone() into anonymous memory and assigning it with
`load_state_dict(assign=True)` keeps the transfer at disk speed.
"""

import json
from pathlib import Path

import torch
from safetensors import safe_open


def _shards(directory: Path) -> list[Path]:
    index_path = directory / "model.safetensors.index.json"
    if index_path.is_file():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        return [directory / name for name in sorted(set(index["weight_map"].values()))]
    return sorted(directory.glob("*.safetensors"))


def load_sharded(model, directory: str | Path, device: str = "cuda",
                 dtype: torch.dtype = torch.bfloat16, log=print) -> None:
    """Fill an already-initialized model with a sharded checkpoint on `device`."""
    directory = Path(directory)
    shards = _shards(directory)
    for number, shard in enumerate(shards, start=1):
        part = {}
        with safe_open(str(shard), framework="pt") as handle:
            for key in handle.keys():
                # clone() first: copying from the mapping itself is what stalls.
                part[key] = handle.get_tensor(key).clone().to(device=device, dtype=dtype)
        model.load_state_dict(part, strict=False, assign=True)
        del part
        if log:
            log(f"  {Path(directory).name} shard {number}/{len(shards)} "
                f"({torch.cuda.memory_allocated() / 2**30:.1f} GiB on device)")


def load_diffusers_gpu(cls, directory: str | Path, device: str = "cuda",
                       dtype: torch.dtype = torch.bfloat16, log=print):
    """Instantiate a Diffusers model on the accelerator and stream its shards in."""
    directory = Path(directory)
    # from_config() honours the global default dtype, which is float32: a 33 B
    # transformer would be allocated at 132 GB and trip the OOM killer.
    previous = torch.get_default_dtype()
    torch.set_default_dtype(dtype)
    try:
        with torch.device(device):
            model = cls.from_config(cls.load_config(str(directory)))
    finally:
        torch.set_default_dtype(previous)
    load_sharded(model, directory, device=device, dtype=dtype, log=log)
    model.eval()
    return model


def load_qwen3vl(path: str | Path, device: str = "cuda", dtype: torch.dtype = torch.bfloat16,
                 log=print):
    """Load a sharded Qwen3-VL text encoder onto the accelerator."""
    from transformers import Qwen3VLConfig, Qwen3VLForConditionalGeneration

    path = Path(path)
    config = Qwen3VLConfig.from_pretrained(str(path), local_files_only=True)
    # Initialized on the accelerator: a meta model would leave buffers (rotary tables,
    # masks) without storage, and a CPU init costs a second full copy.
    with torch.device(device):
        model = Qwen3VLForConditionalGeneration(config)
    load_sharded(model, path, device=device, dtype=dtype, log=log)
    model.eval()
    return model
