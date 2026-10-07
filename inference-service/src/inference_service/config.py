from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    model_root: Path
    inference_token: str
    admin_token: str
    device: str = "cuda:0"
    dtype: str = "bfloat16"
    instance_id: str = "gpu-node-1"

    @classmethod
    def from_env(cls) -> "Settings":
        root = os.environ.get("MODEL_ROOT")
        inference = os.environ.get("INFERENCE_TOKEN")
        admin = os.environ.get("ADMIN_TOKEN")
        if not root or not inference or not admin:
            raise RuntimeError("MODEL_ROOT, INFERENCE_TOKEN and ADMIN_TOKEN are required")
        if inference == admin:
            raise RuntimeError("Inference and admin tokens must differ")
        device = os.environ.get("DEVICE", "cuda:0")
        dtype = os.environ.get("DTYPE", "bfloat16")
        if dtype not in {"float16", "bfloat16", "float32"}:
            raise RuntimeError("DTYPE must be float16, bfloat16 or float32")
        if not (device == "cpu" or device.startswith("cuda:")):
            raise RuntimeError("DEVICE must be cpu or cuda:<index>")
        return cls(Path(root).expanduser().resolve(), inference, admin,
                   device, dtype,
                   os.environ.get("INSTANCE_ID", "gpu-node-1"))
