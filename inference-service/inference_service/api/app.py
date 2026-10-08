"""HTTP routes, auth and response mapping for the inference engine."""

import hmac
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from ..config import Settings
from ..engine.registry import valid_id
from ..engine.service import Engine, ServiceError
from ..schemas import GenerationRequest, GenerationResponse, LifecycleResponse


def create_app(settings: Settings | None = None, backend_factory=None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = Engine(settings, backend_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            if engine.model_id and engine.backend is not None:
                await engine.unload(engine.model_id)

    app = FastAPI(title="Image inference service", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = "req_" + uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else {}
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}},
                            status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse({"error": {"code": "invalid_request", "message": "Request parameters are invalid"}},
                            status_code=400)

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        code = "not_found" if exc.status_code == 404 else "http_error"
        return JSONResponse({"error": {"code": code, "message": str(exc.detail)}},
                            status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        return JSONResponse({"error": {"code": "internal_error", "message": "Internal service error"}},
                            status_code=500)

    def authorize(expected: str, scheme_name: str):
        bearer = HTTPBearer(auto_error=False, scheme_name=scheme_name)

        async def dependency(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
            candidate = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
            if not candidate or not hmac.compare_digest(candidate, expected):
                raise ServiceError(401, "unauthorized", "Invalid service token")
        return dependency

    infer_auth = authorize(settings.inference_token, "InferenceBearer")
    admin_auth = authorize(settings.admin_token, "AdminBearer")

    @app.get("/health/live")
    async def live():
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready():
        if engine.state == "error":
            raise ServiceError(503, "engine_recovering", "Engine requires recovery", 1)
        return {"status": "ready", "boot_id": engine.boot_id}

    @app.get("/v1/models", dependencies=[Depends(infer_auth)])
    async def public_models():
        return {"object": "list", "data": [
            {"id": item.model_id, "object": "model", "created": 0, "owned_by": "local"}
            for item in engine.registry.checkpoints().values()]}

    @app.get("/internal/v1/checkpoints", dependencies=[Depends(admin_auth)])
    async def checkpoints(include_invalid: bool = False):
        return {"checkpoints": [item.public() for item in engine.registry.checkpoints(include_invalid).values()]}

    @app.get("/internal/v1/checkpoints/{model_id}", dependencies=[Depends(admin_auth)])
    async def checkpoint(model_id: str):
        item = engine.registry.checkpoints(include_invalid=True).get(model_id) if valid_id(model_id) else None
        if item is None:
            raise ServiceError(404, "model_not_found", "Model not found")
        return item.public()

    @app.get("/internal/v1/checkpoints/{model_id}/loras", dependencies=[Depends(admin_auth)])
    async def checkpoint_loras(model_id: str):
        if not valid_id(model_id) or model_id not in engine.registry.checkpoints():
            raise ServiceError(404, "model_not_found", "Model not found")
        return {"model_id": model_id, "loras": [item.public() for item in engine.registry.loras(model_id)]}

    @app.get("/internal/v1/checkpoints/{model_id}/loras/{lora_id}", dependencies=[Depends(admin_auth)])
    async def checkpoint_lora(model_id: str, lora_id: str):
        item = engine.registry.lora(model_id, lora_id)
        if item is None:
            raise ServiceError(404, "lora_not_found", "LoRA not found")
        return item.public()

    @app.get("/internal/v1/loras", dependencies=[Depends(admin_auth)])
    async def loras(model_id: str | None = None):
        if model_id is not None and (not valid_id(model_id) or model_id not in engine.registry.checkpoints()):
            raise ServiceError(404, "model_not_found", "Model not found")
        return {"loras": [item.public() for item in engine.registry.loras(model_id)]}

    @app.get("/internal/v1/models", dependencies=[Depends(admin_auth)])
    async def internal_models(loaded: bool = False):
        return engine.snapshot(loaded)

    @app.post("/internal/v1/models/{model_id}/load", dependencies=[Depends(admin_auth)],
              response_model=LifecycleResponse)
    async def load(model_id: str):
        return await engine.load(model_id)

    @app.post("/internal/v1/models/{model_id}/unload", dependencies=[Depends(admin_auth)],
              response_model=LifecycleResponse)
    async def unload(model_id: str):
        return await engine.unload(model_id)

    @app.get("/internal/v1/resources", dependencies=[Depends(admin_auth)])
    async def resources():
        return engine.resources()

    @app.get("/internal/v1/capabilities", dependencies=[Depends(admin_auth)])
    async def capabilities():
        return engine.capabilities()

    @app.post("/v1/images/generations", dependencies=[Depends(infer_auth)],
              response_model=GenerationResponse)
    async def generate(payload: GenerationRequest, request: Request):
        result = await engine.generate(payload)
        result["metadata"]["request_id"] = request.state.request_id
        return result

    original_openapi = app.openapi

    def contract_openapi():
        schema = original_openapi()
        error_schema = {"type": "object", "required": ["error"], "properties": {
            "error": {"type": "object", "required": ["code", "message"], "properties": {
                "code": {"type": "string"}, "message": {"type": "string"}}}}}
        schema["components"]["schemas"]["ErrorResponse"] = error_schema
        error_content = {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}
        for path, path_item in schema["paths"].items():
            for operation in path_item.values():
                responses = operation["responses"]
                if "422" in responses:
                    responses.pop("422")
                    responses["400"] = {"description": "Invalid request", "content": error_content}
                if operation.get("security"):
                    responses["401"] = {"description": "Invalid service token", "content": error_content}
                if path == "/v1/images/generations" or path.endswith("/load") or path.endswith("/unload"):
                    for code, description in (("404", "Resource not found"),
                                              ("409", "Model state conflict"),
                                              ("500", "Execution failed"),
                                              ("503", "Engine busy or recovering")):
                        responses[code] = {"description": description, "content": error_content}
        return schema

    app.openapi = contract_openapi
    return app
