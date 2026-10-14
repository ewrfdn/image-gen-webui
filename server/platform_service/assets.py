"""Reference storage and conversion; inference receives base64, never URLs."""
import asyncio
import base64
import hashlib
import io
import httpx
from urllib.parse import urlsplit
from PIL import Image
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .models import Asset, Blob, MediaFile, uid
from .errors import ApiError
from .storage import write_atomic


def inspect_image(data):
    if len(data) > 20 * 1024 * 1024:
        raise ApiError(400, "file_too_large", "Reference limit is 20 MB per file")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            mime = Image.MIME[image.format]
        if mime not in ("image/png", "image/jpeg", "image/webp"):
            raise ValueError()
    except Exception as exc:
        raise ApiError(400, "invalid_reference", "Reference must be a valid PNG, JPEG or WebP") from exc
    return {"data": data, "mime": mime, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class AssetService:
    def __init__(self, platform):
        self.platform = platform
        self.lock = asyncio.Lock()

    async def upload(self, owner, data):
        info = await asyncio.to_thread(inspect_image, data)
        digest = info["sha256"]
        # Unique constraints also protect concurrent uploads across processes.
        async with self.lock:
            for attempt in range(3):
                async with self.platform.db.sessions() as db:
                    blob = await db.scalar(select(Blob).where(Blob.sha256 == digest))
                    if blob is None:
                        blob = Blob(id=uid(), sha256=digest, storage_key=f"blobs/{digest}", mime=info["mime"], size=len(data))
                        target = self.platform.path(blob.storage_key)
                        if not target.exists():
                            await asyncio.to_thread(write_atomic, target, data)
                        db.add(blob)
                    asset = await db.scalar(select(Asset).where(Asset.owner == owner, Asset.blob_id == blob.id))
                    if asset is None:
                        asset = Asset(id=uid(), owner=owner, blob_id=blob.id)
                        db.add(asset)
                    try:
                        await db.commit()
                    except IntegrityError:
                        await db.rollback()
                        if attempt == 2:
                            raise
                        continue
                    return {"id": asset.id, "blob_id": blob.id, "sha256": digest, "mime": blob.mime,
                            "size": blob.size, "uri": f"assets://{asset.id}", "url": f"/api/assets/{asset.id}/content"}

    async def get(self, owner, asset_id, admin=False):
        async with self.platform.db.sessions() as db:
            asset = await db.get(Asset, asset_id)
            if asset is None or (not admin and asset.owner != owner):
                raise ApiError(404, "asset_not_found", "Asset not found")
            return await db.get(Blob, asset.blob_id)

    async def resolve(self, owner, value, capability):
        if value.startswith("assets://"):
            blob = await self.get(owner, value[9:])
            data = await asyncio.to_thread(self.platform.path(blob.storage_key).read_bytes)
        elif value.startswith(("http://", "https://")):
            # Authenticated local references are resolved without an HTTP round trip.
            path = urlsplit(value).path
            if path.startswith("/api/assets/") and path.endswith("/content"):
                blob = await self.get(owner, path.split("/")[3])
                data = await asyncio.to_thread(self.platform.path(blob.storage_key).read_bytes)
            else:
                try:
                    return await self.fetch(value, capability)
                except httpx.HTTPError as exc:
                    raise ApiError(400, "invalid_reference", "Reference URL could not be fetched") from exc
        else:
            if len(value) > 28 * 1024 * 1024:
                raise ApiError(400, "file_too_large", "Reference exceeds upload limit")
            async with self.platform.db.sessions() as db:
                asset = await db.get(Asset, value)
                legacy = await db.get(MediaFile, value) if asset is None else None
            if asset:
                blob = await self.get(owner, value)
                data = await asyncio.to_thread(self.platform.path(blob.storage_key).read_bytes)
            elif legacy and legacy.owner == owner and legacy.role == "reference" and not legacy.task_id:
                data = await asyncio.to_thread(self.platform.path(legacy.storage_key).read_bytes)
            else:
                try:
                    data = base64.b64decode(value.split(",", 1)[1] if value.startswith("data:") else value, validate=True)
                except ValueError as exc:
                    raise ApiError(400, "invalid_reference", "Invalid reference or upload ID") from exc
        return await self.validate(data, capability)

    async def validate(self, data, capability):
        info = await asyncio.to_thread(inspect_image, data)
        if info["mime"] not in capability["mime_types"] or info["size"] > capability["max_file_bytes"]:
            raise ApiError(400, "invalid_reference", "Reference type or size is outside model limits")
        return info

    async def fetch(self, value, capability):
        async with self.platform.client.stream("GET", value, timeout=30) as response:
            response.raise_for_status()
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > min(capability["max_file_bytes"], 20 * 1024 * 1024):
                    raise ApiError(400, "file_too_large", "Reference exceeds model limit")
            data = bytes(chunks)
        return await self.validate(data, capability)
