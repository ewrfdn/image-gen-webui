"""Compatibility shim: the loaders live with the backend that uses them.

Kept so the standalone baseline scripts keep working without duplicating the
weight-staging logic.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inference_service.backends.minimax_h3_image.loading import (  # noqa: E402,F401
    load_diffusers_gpu,
    load_qwen3vl,
    load_sharded,
)
