"""ASGI entry point: uvicorn inference_service.app:create_app --factory."""

from .api.app import create_app

__all__ = ["create_app"]
