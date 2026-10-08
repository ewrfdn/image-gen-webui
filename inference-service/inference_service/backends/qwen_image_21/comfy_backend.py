"""Own an isolated ComfyUI process for the three-file Qwen-Image 2.1 layout."""

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

from ...config import Settings
from ...resources import Checkpoint, Lora, QWEN_COMFY_FILES
from ...schemas import GenerationRequest
from ..base import UnsupportedParameter


class ComfyQwenImage21Backend:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.process: subprocess.Popen | None = None
        self.workspace: tempfile.TemporaryDirectory | None = None
        self.log = None
        self.base_url: str | None = None

    @staticmethod
    def workflow(prompt: str, negative_prompt: str, width: int, height: int,
                 seed: int, steps: int, guidance: float, prefix: str) -> dict:
        return {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": QWEN_COMFY_FILES["transformer"].name, "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": QWEN_COMFY_FILES["text_encoder"].name, "type": "qwen_image", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_COMFY_FILES["vae"].name}},
            "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"prompt": prompt, "negative_prompt": negative_prompt, "resolution": 1024, "clip": ["2", 0]}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
            "6": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": steps, "cfg": guidance, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0, "model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1], "latent_image": ["5", 0]}},
            "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
            "8": {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix, "images": ["7", 0]}},
        }

    def _request(self, route: str, payload: dict | None = None, timeout: float = 10) -> bytes:
        if self.base_url is None:
            raise RuntimeError("private ComfyUI is not running")
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base_url + route, data=data,
                          headers={"Content-Type": "application/json"} if data else {})
        with urlopen(request, timeout=timeout) as response:
            return response.read()

    def _run_workflow(self, graph: dict, timeout: float = 900) -> bytes:
        response = json.loads(self._request("/prompt", {"prompt": graph, "client_id": uuid.uuid4().hex}))
        prompt_id = response["prompt_id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError("private ComfyUI exited during generation")
            history = json.loads(self._request("/history/" + prompt_id))
            entry = history.get(prompt_id)
            if entry:
                if entry.get("status", {}).get("status_str") == "error":
                    raise RuntimeError("private ComfyUI reported generation error")
                images = entry.get("outputs", {}).get("8", {}).get("images", [])
                if images:
                    image = images[0]
                    if image.get("type") != "output" or image.get("subfolder") not in ("", None):
                        raise RuntimeError("unexpected private ComfyUI output location")
                    filename = image["filename"]
                    if Path(filename).name != filename:
                        raise RuntimeError("unexpected private ComfyUI filename")
                    png = self._request("/view?" + urlencode({"filename": filename, "type": "output"}), timeout=60)
                    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise RuntimeError("private ComfyUI returned invalid PNG")
                    if self.workspace is not None:
                        (Path(self.workspace.name) / "output" / filename).unlink(missing_ok=True)
                    return png
            time.sleep(0.5)
        raise TimeoutError("private ComfyUI generation timed out")

    def load(self, checkpoint: Checkpoint) -> None:
        root = self.settings.comfyui_root
        python = self.settings.comfyui_python
        if root is None or python is None or not (root / "main.py").is_file() or not python.is_file():
            raise RuntimeError("COMFYUI_ROOT and COMFYUI_PYTHON must identify an installed ComfyUI")
        for relative in QWEN_COMFY_FILES.values():
            actual = root / "models" / relative
            expected = checkpoint.path / relative
            if not actual.is_file() or actual.resolve() != expected.resolve():
                raise RuntimeError("ComfyUI model paths must resolve to the discovered MODEL_ROOT files")
        self.workspace = tempfile.TemporaryDirectory(prefix="inference-comfy-qwen-")
        work = Path(self.workspace.name)
        for name in ("user", "output", "input", "temp"):
            (work / name).mkdir()
        self.log = (work / "comfy.log").open("wb")
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            port = candidate.getsockname()[1]
        flags = ["--lowvram"] if self.settings.model_offload == "model_cpu" else (["--novram"] if self.settings.model_offload == "sequential_cpu" else [])
        if self.settings.device == "cpu":
            flags.append("--cpu")
        else:
            flags.extend(("--cuda-device", self.settings.device.split(":", 1)[1]))
        self.process = subprocess.Popen(
            [str(python), str(root / "main.py"), "--listen", "127.0.0.1", "--port", str(port),
             "--disable-all-custom-nodes", "--user-directory", str(work / "user"),
             "--output-directory", str(work / "output"), "--input-directory", str(work / "input"),
             "--temp-directory", str(work / "temp"), *flags],
            cwd=root, stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
            start_new_session=(os.name == "posix"))
        self.base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("private ComfyUI failed to start")
            try:
                self._request("/system_stats", timeout=2)
                break
            except (URLError, TimeoutError, ConnectionError):
                time.sleep(0.5)
        else:
            raise TimeoutError("private ComfyUI startup timed out")
        # A successful HTTP startup alone does not mean the weights are resident.
        self._run_workflow(self.workflow("warmup", "", 512, 512, 0, 1, 1.0, "warmup"))

    def generate(self, request: GenerationRequest, lora: Lora | None, seed: int) -> bytes:
        if lora is not None or request.loras:
            raise UnsupportedParameter("LoRA is not supported for ComfyUI Qwen-Image 2.1 weights")
        guidance = request.guidance_scale if request.guidance_scale is not None else 1.0
        if request.negative_prompt and guidance <= 1:
            raise UnsupportedParameter("negative_prompt requires guidance_scale > 1 for Qwen-Image 2.1")
        width, height = (int(part) for part in request.size.split("x"))
        graph = self.workflow(request.prompt, request.negative_prompt or "", width, height, seed,
                              request.num_inference_steps or 40, guidance, "result_" + uuid.uuid4().hex)
        return self._run_workflow(graph)

    def unload(self) -> None:
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=10)
        finally:
            self.process = None
            self.base_url = None
            if self.log is not None:
                self.log.close()
                self.log = None
            if self.workspace is not None:
                self.workspace.cleanup()
                self.workspace = None
