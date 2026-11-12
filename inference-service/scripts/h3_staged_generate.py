"""Full-precision MiniMax-H3-Image baseline with staged component loading.

The 135 GB checkpoint cannot be resident on a 128 GB DGX Spark, so this runner keeps
exactly one component group loaded at a time and hands the intermediates (prompt
embeddings, reference latents, denoising latents) through host memory or disk:

    encode   text_encoder + tokenizer + processor (+ vae for edit references)  ~65 GB
    denoise  transformer + scheduler                                          ~63 GB
    decode   vae + image_processor                                            ~3 GB

Run all stages in one process, or one stage per process with --stage/--workdir.
The script refuses to start a stage when the free memory is below --min-free-gb.

    python scripts/h3_staged_generate.py --prompt-file p.txt --size 2048x2048 --out base.png
"""

import argparse
import gc
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULTS = {"min_generation_area": 4194304, "output_type": "pil"}
STATE_KEYS = ("prompt_embeds", "text_token_tags", "reference_latents", "reference_noise",
              "latents", "height", "width", "output_height", "output_width", "min_generation_area")


def memory_report(stage: str, started: float) -> None:
    import torch
    try:
        import psutil
        rss = f"{psutil.Process().memory_info().rss / 2**30:.1f} GiB RSS"
    except ImportError:
        rss = "RSS n/a"
    if torch.cuda.is_available():
        reserved = f"{torch.cuda.max_memory_reserved() / 2**30:.1f} GiB CUDA reserved"
        torch.cuda.reset_peak_memory_stats()
    else:
        reserved = "no CUDA"
    print(f"[{stage}] {time.perf_counter() - started:.1f} s | {rss} | {reserved}", flush=True)


def free_gb() -> float:
    import psutil
    return psutil.virtual_memory().available / 2**30


def load(pipe, names, min_free_gb: float, stage: str) -> float:
    available = free_gb()
    if available < min_free_gb:
        raise SystemExit(
            f"stage {stage} needs about {min_free_gb:.0f} GiB free, only {available:.1f} GiB available; "
            "stop the other GPU workloads first")
    import torch
    started = time.perf_counter()
    try:
        # device_map streams each shard straight onto the accelerator, so the host
        # never holds a second copy of the component (unified memory on GB10).
        pipe.load_components(names=names, dtype=torch.bfloat16, device_map="cuda")
    except TypeError:
        pipe.load_components(names=names, dtype=torch.bfloat16)
        for name in names:
            component = getattr(pipe, name, None)
            if component is not None and hasattr(component, "to"):
                component.to("cuda")
    print(f"[{stage}] loaded {', '.join(names)} in {time.perf_counter() - started:.1f} s", flush=True)
    return started


def unload(pipe, names) -> None:
    import torch
    pipe.unload_components(names=names)
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


# `before_denoise` builds its row-index tensors on the CPU and only then moves them to
# the execution device, so `text_token_tags` has to stay on the CPU; every other staged
# tensor is a model input and has to be on the accelerator.
CPU_KEYS = {"text_token_tags"}


def to_device(value, device="cuda", key=None):
    """Move every tensor of a staged payload onto the accelerator, except CPU-only keys."""
    import torch
    if torch.is_tensor(value):
        return value if key in CPU_KEYS else value.to(device)
    if isinstance(value, dict):
        return {name: to_device(item, device, name) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        moved = [to_device(item, device, key) for item in value]
        return type(value)(moved) if isinstance(value, tuple) else moved
    return value

def encode(pipe, args, workdir: Path) -> dict:
    import torch
    from PIL import Image
    from diffusers.modular_pipelines.modular_pipeline import PipelineState

    references = [Image.open(path).convert("RGB") for path in args.image]
    names = ["tokenizer", "processor"]
    if references:
        names.append("vae")
    started = load(pipe, names, args.min_free_gb, "encode")
    # The text encoder goes through the staging loader: transformers' own path stalls.
    from h3_loader import load_qwen3vl
    encoder = load_qwen3vl(args.checkpoint / "text_encoder", device="cuda",
                           log=lambda message: print(message, flush=True))
    pipe.update_components(text_encoder=encoder)

    state = PipelineState()
    state.set("prompt", args.prompt)
    state.set("height", args.height)
    state.set("width", args.width)
    state.set("min_generation_area", args.min_generation_area)
    state.set("generator", torch.Generator("cpu").manual_seed(args.seed))
    if references:
        state.set("image", references if len(references) > 1 else references[0])

    pipe, state = pipe.blocks.sub_blocks["before_encode"](pipe, state)
    pipe, state = pipe.blocks.sub_blocks["text_encoder"](pipe, state)
    if references:
        pipe, state = pipe.blocks.sub_blocks["vae_encoder"](pipe, state)

    payload = {key: state.get(key) for key in STATE_KEYS if state.get(key) is not None}
    torch.save(payload, workdir / "encode.pt")
    memory_report("encode", started)
    pipe.update_components(text_encoder=None)
    del encoder
    unload(pipe, names)
    return payload


def denoise(pipe, args, workdir: Path) -> dict:
    import torch
    from diffusers.modular_pipelines.modular_pipeline import PipelineState

    payload = to_device(torch.load(workdir / "encode.pt", map_location="cpu", weights_only=False))
    started = load(pipe, ["scheduler"], args.min_free_gb, "denoise")
    from diffusers import MiniMaxH3ImageTransformer2DModel
    from h3_loader import load_diffusers_gpu
    transformer = load_diffusers_gpu(MiniMaxH3ImageTransformer2DModel,
                                     args.checkpoint / "transformer_diffusers", device="cuda",
                                     log=lambda message: print(message, flush=True))
    pipe.update_components(transformer=transformer)

    state = PipelineState()
    for key, value in payload.items():
        state.set(key, value)
    state.set("generator", torch.Generator("cpu").manual_seed(args.seed))
    state.set("height", args.height)
    state.set("width", args.width)
    state.set("min_generation_area", args.min_generation_area)

    pipe, state = pipe.blocks.sub_blocks["denoise"](pipe, state)
    result = {key: state.get(key) for key in ("latents", "output_height", "output_width")}
    torch.save(result, workdir / "denoise.pt")
    memory_report("denoise", started)
    pipe.update_components(transformer=None)
    del transformer
    unload(pipe, ["scheduler"])
    return result


def decode(pipe, args, workdir: Path) -> Path:
    import torch
    from diffusers.modular_pipelines.modular_pipeline import PipelineState

    payload = torch.load(workdir / "denoise.pt", map_location="cpu", weights_only=False)
    started = load(pipe, ["vae", "image_processor"], args.min_free_gb, "decode")

    state = PipelineState()
    for key, value in payload.items():
        state.set(key, value)
    state.set("output_type", DEFAULTS["output_type"])

    pipe, state = pipe.blocks.sub_blocks["decode"](pipe, state)
    images = state.get("images")
    if not images:
        raise SystemExit("decode produced no image")
    images[0].save(args.out, format="PNG")
    memory_report("decode", started)
    pipe.update_components(vae=None)
    unload(pipe, ["vae"])
    return Path(args.out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, default=Path("~/models/MiniMax-H3-Image"))
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--prompt-file", type=Path, default=None)
    parser.add_argument("--image", action="append", default=[], type=Path,
                        help="reference image; repeat for <Picture 2>, ...")
    parser.add_argument("--size", default="2048x2048")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-generation-area", type=int, default=DEFAULTS["min_generation_area"])
    parser.add_argument("--stage", choices=("all", "encode", "denoise", "decode"), default="all")
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/h3-staged"))
    parser.add_argument("--min-free-gb", type=float, default=70.0)
    parser.add_argument("--out", type=Path, default=Path("h3-baseline.png"))
    args = parser.parse_args()

    if args.prompt_file:
        args.prompt = args.prompt_file.read_text(encoding="utf-8").strip()
    if not args.prompt:
        raise SystemExit("--prompt or --prompt-file is required")
    args.width, args.height = (int(part) for part in args.size.lower().split("x"))

    from diffusers import ModularPipeline
    args.checkpoint = args.checkpoint.expanduser().resolve()
    if not (args.checkpoint / "modular_model_index.json").is_file():
        raise SystemExit(f"run scripts/prepare_h3_modular.py --checkpoint {args.checkpoint} first")

    args.workdir.mkdir(parents=True, exist_ok=True)
    (args.workdir / "run.json").write_text(json.dumps({
        "checkpoint": str(args.checkpoint), "size": args.size, "seed": args.seed,
        "prompt": args.prompt, "images": [str(path) for path in args.image],
    }, indent=2), encoding="utf-8")

    pipe = ModularPipeline.from_pretrained(args.checkpoint, local_files_only=True)
    started = time.perf_counter()
    if args.stage in ("all", "encode"):
        encode(pipe, args, args.workdir)
    if args.stage in ("all", "denoise"):
        denoise(pipe, args, args.workdir)
    if args.stage in ("all", "decode"):
        print("wrote", decode(pipe, args, args.workdir))
    print(f"total {time.perf_counter() - started:.1f} s", flush=True)


if __name__ == "__main__":
    main()
