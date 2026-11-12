"""Add the Diffusers modular metadata the official MiniMax-H3-Image snapshot lacks.

The Hub snapshot ships `model_index.json` with `_class_name: MiniMaxH3ImagePipeline`,
which is the SGLang/vLLM loading contract; Diffusers resolves `modular_model_index.json`
instead. This script writes that file plus a scheduler config, so the checkpoint loads
with `ModularPipeline.from_pretrained(<checkpoint>)` without any conversion.

    python scripts/prepare_h3_modular.py --checkpoint ~/models/MiniMax-H3-Image
"""

import argparse
import json
from pathlib import Path

COMPONENTS = {
    "processor": ("transformers", "Qwen3VLProcessor"),
    "tokenizer": ("transformers", "Qwen2TokenizerFast"),
    "text_encoder": ("transformers", "Qwen3VLForConditionalGeneration"),
    "transformer": ("diffusers", "MiniMaxH3ImageTransformer2DModel"),
    "vae": ("diffusers", "AutoencoderKLMiniMaxH3Image"),
    "scheduler": ("diffusers", "MiniMaxH3Scheduler"),
}

DEFAULT_SIGMAS = [1.0, 0.9675236940383911, 0.8875485062599182, 0.75,
                  0.5327739119529724, 0.23200887441635132, 0.0]


def sigmas(checkpoint: Path) -> list:
    path = checkpoint / "pdd_config.json"
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get("sigmas")
        if isinstance(values, list) and len(values) >= 2:
            return [float(value) for value in values]
    return list(DEFAULT_SIGMAS)


def missing_components(checkpoint: Path) -> list:
    """Components whose weights the snapshot actually ships."""
    present = [name for name in COMPONENTS if (checkpoint / name).is_dir()]
    return [name for name in COMPONENTS if name not in present]


def build(checkpoint: Path, root: str | None = None) -> dict:
    target = root or str(checkpoint)
    index = {
        "_class_name": "MiniMaxH3ImageModularPipeline",
        "_diffusers_version": "0.42.0.dev0",
        "_blocks_class_name": "MiniMaxH3ImageBlocks",
        "sample_sigmas": sigmas(checkpoint),
    }
    for name, (library, class_name) in COMPONENTS.items():
        index[name] = [library, class_name, {
            "type_hint": [library, class_name],
            "pretrained_model_name_or_path": target,
            "subfolder": name,
            "variant": None,
            "revision": None,
        }]
    return index


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--root", help="path or Hub id written into the component specs (default: the checkpoint)")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    checkpoint = args.checkpoint.expanduser().resolve()
    index_path = checkpoint / "modular_model_index.json"
    if index_path.exists() and not args.force:
        print(f"{index_path} already exists; pass --force to rewrite")
    else:
        index_path.write_text(json.dumps(build(checkpoint, args.root), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {index_path}")

    scheduler = checkpoint / "scheduler"
    scheduler_config = scheduler / "scheduler_config.json"
    if not scheduler_config.exists():
        scheduler.mkdir(parents=True, exist_ok=True)
        scheduler_config.write_text(json.dumps({
            "_class_name": "MiniMaxH3Scheduler",
            "shift": 12.0,
            "stochastic_sampling": False,
            "shift_provided_sigmas": None,
            "euler_update_form": "blend",
        }, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {scheduler_config}")

    missing = missing_components(checkpoint)
    print("weights shipped for:", ", ".join(name for name in COMPONENTS if name not in missing) or "none")
    if missing:
        print("absent subfolders (will load as null components):", ", ".join(missing))


if __name__ == "__main__":
    main()
