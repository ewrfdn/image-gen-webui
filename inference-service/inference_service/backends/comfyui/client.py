"""Minimal stdlib HTTP client for a local ComfyUI server (no extra dependencies)."""

import json
import socket
import time
import urllib.error
import urllib.request
import uuid

from ..base import BackendUnavailable


class ComfyUIUnavailable(BackendUnavailable):
    """The ComfyUI server is unreachable, refused a call or could not run the prompt."""


def _failure_message(entry: dict) -> str:
    """Pull the execution_error message out of a ComfyUI history entry."""
    for message in (entry.get("status") or {}).get("messages") or []:
        if isinstance(message, (list, tuple)) and len(message) == 2 and message[0] == "execution_error":
            detail = message[1] if isinstance(message[1], dict) else {}
            text = detail.get("exception_message") or detail.get("exception_type")
            if text:
                return str(text).strip()[:400]
    return "ComfyUI failed while executing the workflow"


class ComfyUIClient:
    def __init__(self, base_url: str, timeout: float = 900.0, poll_interval: float = 0.5):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval

    def _request(self, path: str, *, data: bytes | None = None,
                 headers: dict | None = None, method: str | None = None,
                 timeout: float | None = None) -> bytes:
        if not self.base_url:
            raise ComfyUIUnavailable("COMFYUI_URL is not configured")
        request = urllib.request.Request(self.base_url + path, data=data,
                                         headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read()[:300].decode("utf-8", "replace")
            raise ComfyUIUnavailable(f"ComfyUI {path} returned HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as error:
            raise ComfyUIUnavailable(f"ComfyUI {path} is unreachable: {error}") from error

    def object_info(self, *node_ids: str) -> dict:
        """Return each node's schema; every requested id must exist."""
        info = json.loads(self._request("/object_info", timeout=30))
        missing = [node for node in node_ids if node not in info]
        if missing:
            raise ComfyUIUnavailable("ComfyUI lacks required nodes: " + ", ".join(missing))
        return {node: info[node] for node in node_ids}

    def choice_values(self, node_info: dict, field: str) -> list:
        """Read the allowed values of a loader field such as unet_name."""
        try:
            spec = node_info["input"]["required"][field]
        except (KeyError, TypeError):
            return []
        values = spec[0] if isinstance(spec, (list, tuple)) and spec else spec
        return list(values) if isinstance(values, (list, tuple)) else []

    def upload_image(self, name: str, png: bytes) -> str:
        """Upload one input image and return the value LoadImage expects."""
        boundary = "----imagegen" + uuid.uuid4().hex
        body = b"".join([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\ninput\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
            "Content-Type: image/png\r\n\r\n".encode(),
            png,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        raw = self._request("/upload/image", data=body, method="POST", timeout=60,
                            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        payload = json.loads(raw)
        uploaded = payload.get("name")
        if not isinstance(uploaded, str) or not uploaded:
            raise ComfyUIUnavailable("ComfyUI rejected the reference upload")
        subfolder = payload.get("subfolder") or ""
        return f"{subfolder}/{uploaded}" if subfolder else uploaded

    def enqueue(self, graph: dict) -> str:
        body = json.dumps({"prompt": graph, "client_id": uuid.uuid4().hex}).encode()
        raw = self._request("/prompt", data=body, method="POST", timeout=60,
                            headers={"Content-Type": "application/json"})
        payload = json.loads(raw)
        if payload.get("node_errors"):
            raise ComfyUIUnavailable(f"ComfyUI rejected the workflow: {json.dumps(payload['node_errors'])[:300]}")
        prompt_id = payload.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ComfyUIUnavailable("ComfyUI did not return a prompt id")
        return prompt_id

    def wait(self, prompt_id: str, deadline: float) -> dict:
        """Poll the history endpoint until the prompt finished or the deadline passed."""
        while True:
            raw = self._request(f"/history/{prompt_id}", timeout=30)
            history = json.loads(raw)
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    detail = _failure_message(entry)
                    if "out of memory" in detail.lower():
                        # The engine turns this into gpu_oom and marks the slot unusable.
                        raise RuntimeError(f"ComfyUI ran out of memory: {detail}")
                    raise ComfyUIUnavailable(detail)
                if status.get("completed") or entry.get("outputs"):
                    return entry
            if time.monotonic() >= deadline:
                raise ComfyUIUnavailable("ComfyUI generation timed out")
            time.sleep(self.poll_interval)

    def image(self, filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
        query = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": folder_type})
        return self._request(f"/view?{query}", timeout=120)

    def free(self) -> None:
        body = json.dumps({"unload_models": True, "free_memory": True}).encode()
        self._request("/free", data=body, method="POST", timeout=60,
                      headers={"Content-Type": "application/json"})
