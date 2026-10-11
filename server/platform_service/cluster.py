"""Shared in-process registry. Queries have a single absolute one-second deadline."""

import asyncio
import copy
import time
import json
from pathlib import Path
from urllib.parse import quote
import httpx
from cryptography.fernet import Fernet
from jsonschema import Draft202012Validator
from sqlalchemy import select
from .db import Node, Task


class Registry:
    def __init__(self, platform):
        self.platform = platform
        self.snapshots = {}
        self.errors = {}
        self.reserved = set()
        self.quarantined = set()
        self.last_invoked = {}
        self.lock = asyncio.Lock()
        self.cipher = Fernet(platform.config.encryption_key.encode())
        self.client = platform.client
        self.initialized = False
        self.validator = Draft202012Validator(
            json.loads(
                (
                    Path(__file__).resolve().parents[2]
                    / "design/model-config.schema.json"
                ).read_text(encoding="utf-8")
            )
        )

    def encrypt(self, value):
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value):
        return self.cipher.decrypt(value.encode()).decode()

    def headers(self, node, admin=True):
        return {
            "Authorization": "Bearer "
            + self.decrypt(node.admin_secret if admin else node.inference_secret)
        }

    async def node_request(self, node, method, endpoint, *, timeout=1, **kwargs):
        response = await self.client.request(
            method,
            node.base_url.rstrip("/") + endpoint,
            headers=self.headers(node, admin=endpoint.startswith("/internal")),
            timeout=timeout,
            **kwargs,
        )
        response.raise_for_status()
        return response

    async def sync(self):
        async with self.platform.db.sessions() as db:
            nodes = list(
                (await db.scalars(select(Node).where(Node.enabled.is_(True)))).all()
            )
            unknown = (
                await db.scalars(select(Task.node_id).where(Task.status == "unknown"))
            ).all()
        self.quarantined = {n for n in unknown if n}
        allowed = {n.id for n in nodes}
        for node_id in list(self.snapshots):
            if node_id not in allowed:
                self.snapshots.pop(node_id)

        async def one(node):
            try:
                async with asyncio.timeout(1):
                    result = (
                        await self.node_request(node, "GET", "/internal/v1/catalog")
                    ).json()
                if not isinstance(result.get("models"), list) or not isinstance(
                    result.get("runtime"), dict
                ):
                    raise ValueError("Invalid catalogue response")
                # Ignore malformed resources rather than exposing unusable UI choices.
                result["models"] = [m for m in result["models"] if self.valid_model(m)]
                result.update(node=node, observed=time.monotonic())
                self.snapshots[node.id] = result
                self.errors.pop(node.id, None)
                for runtime in result["runtime"].get("models", []):
                    if runtime.get("state") == "ready":
                        model = next(
                            (
                                m
                                for m in result["models"]
                                if m["model_id"] == runtime["model_id"]
                            ),
                            None,
                        )
                        if model:
                            self.last_invoked.setdefault(
                                (model["model_id"], model["revision"]), time.monotonic()
                            )
            except Exception as exc:
                self.errors[node.id] = type(exc).__name__

        await asyncio.gather(*(one(n) for n in nodes))
        self.initialized = True

    def valid_model(self, model):
        try:
            c = model["configuration"]
            if not self.validator.is_valid(c):
                return False
            r, g = c["reference"], c["generation"]
            if (
                g["width_min"] > g["width_max"]
                or g["height_min"] > g["height_max"]
                or g["default_steps"] > g["steps_max"]
            ):
                return False
            if not valid_size(g["default_size"], g):
                return False
            if (
                c["type"] == "video"
                and g["video"]["default_duration_seconds"]
                > g["video"]["max_duration_seconds"]
            ):
                return False
            return (
                model.get("local_available", True)
                and c["model_id"] == model["model_id"]
                and c["type"] in ("image", "video")
                and model["revision"] == c["revision"]
                and len(model["artifact_sha256"]) == 64
                and model["artifact_sha256"] != "0" * 64
                and c["artifact_sha256"] == model["artifact_sha256"]
                and 0 <= r["min_count"] <= r["max_count"]
                and isinstance(r["mime_types"], list)
                and g["timeout_seconds"] > 0
                and bool(g["default_size"])
            )
        except (KeyError, TypeError, ValueError):
            return False

    def fresh(self):
        return {
            n: s
            for n, s in self.snapshots.items()
            if time.monotonic() - s["observed"]
            <= self.platform.settings["stale_seconds"]
            and s["node"].enabled
        }

    def models(self):
        grouped = {}
        for node_id, snap in self.fresh().items():
            for item in snap["models"]:
                key = (item["model_id"], item["revision"])
                if key not in grouped:
                    grouped[key] = {
                        **copy.deepcopy(item),
                        "nodes": [],
                        "loras": [],
                        "status": "unavailable",
                        "conflict": False,
                    }
                entry = grouped[key]
                if entry["artifact_sha256"] != item["artifact_sha256"]:
                    entry["conflict"] = True
                entry["nodes"].append(node_id)
                for lora in item.get("loras", []):
                    if lora not in entry["loras"]:
                        entry["loras"].append(copy.deepcopy(lora))
                local = self.local_status(node_id, snap["runtime"], item["model_id"])
                priority = {"unavailable": 0, "busy": 1, "need_load": 2, "available": 3}
                if priority[local] > priority[entry["status"]]:
                    entry["status"] = local
        for item in grouped.values():
            if item["conflict"]:
                item["status"] = "unavailable"
        return list(grouped.values())

    def local_status(self, node_id, runtime, model_id):
        if node_id in self.quarantined:
            return "unavailable"
        rows = runtime.get("models", [])
        if (
            node_id in self.reserved
            or runtime.get("busy")
            or any(r.get("active_requests") for r in rows)
        ):
            return "busy"
        resident = [r for r in rows if r.get("state") not in ("unloaded", None)]
        if any(
            r.get("model_id") == model_id and r.get("can_generate") for r in resident
        ):
            return "available"
        return "busy" if resident else "need_load"

    async def availability(
        self, model_id, revision=None, loras=None, references=None, size=None
    ):
        candidates = []
        aggregate = [
            m
            for m in self.models()
            if m["model_id"] == model_id
            and (revision is None or m["revision"] == revision)
        ]
        if len(aggregate) != 1 or aggregate[0]["conflict"]:
            return {
                "status": "unavailable",
                "node_id": None,
                "reason": "missing_or_ambiguous_model",
            }
        target = aggregate[0]
        for node_id, snap in self.fresh().items():
            item = next(
                (
                    m
                    for m in snap["models"]
                    if m["model_id"] == model_id
                    and m["revision"] == target["revision"]
                    and m["artifact_sha256"] == target["artifact_sha256"]
                ),
                None,
            )
            if item is None:
                continue
            if any(
                not any(
                    l["lora_id"] == wanted["lora_id"]
                    and (
                        not wanted.get("sha256") or l.get("sha256") == wanted["sha256"]
                    )
                    for l in item.get("loras", [])
                )
                for wanted in loras or []
            ):
                continue
            cfg = item["configuration"]
            refs = references or []
            if not cfg["reference"]["min_count"] <= len(refs) <= cfg["reference"][
                "max_count"
            ] or any(
                r["mime"] not in cfg["reference"]["mime_types"]
                or r.get("size", 0) > cfg["reference"]["max_file_bytes"]
                for r in refs
            ):
                continue
            if size and not valid_size(size, cfg["generation"]):
                continue
            candidates.append((node_id, snap))
        results = []

        async def probe(node_id, snap):
            try:
                result = (
                    await self.node_request(snap["node"], "GET", "/internal/v1/models")
                ).json()
                results.append(
                    {
                        "status": self.local_status(node_id, result, model_id),
                        "node_id": node_id,
                    }
                )
            except (httpx.HTTPError, ValueError):
                pass

        try:
            async with asyncio.timeout(1):
                await asyncio.gather(*(probe(n, s) for n, s in candidates))
        except TimeoutError:
            pass
        for status in ("available", "need_load", "busy"):
            found = next((r for r in results if r["status"] == status), None)
            if found:
                return {**found, "revision": target["revision"], "reason": status}
        return {
            "status": "unavailable",
            "node_id": None,
            "reason": "no_matching_node_or_timeout",
        }


def valid_size(size, gen):
    try:
        w, h = map(int, size.split("x"))
        return (
            gen["width_min"] <= w <= gen["width_max"]
            and gen["height_min"] <= h <= gen["height_max"]
            and w * h <= gen["max_pixels"]
            and w % gen["dimension_multiple"] == h % gen["dimension_multiple"] == 0
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
