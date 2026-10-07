from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoraSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lora_id: str = Field(min_length=1, max_length=128)
    scale: float = Field(default=1.0, ge=0.0, le=2.0, allow_inf_nan=False)


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=4000)
    size: str = "1024x1024"
    n: Literal[1] = 1
    response_format: Literal["b64_json"] = "b64_json"
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    num_inference_steps: int = Field(default=25, ge=1, le=100)
    guidance_scale: float = Field(default=7.0, ge=0, le=20, allow_inf_nan=False)
    negative_prompt: str | None = Field(default=None, max_length=4000)
    loras: list[LoraSelection] = Field(default_factory=list, max_length=1)

    @field_validator("size")
    @classmethod
    def validate_size(cls, value: str) -> str:
        try:
            width, height = (int(part) for part in value.split("x"))
        except (ValueError, AttributeError):
            raise ValueError("size must be WIDTHxHEIGHT") from None
        if width < 512 or height < 512 or width > 1024 or height > 1024 or width * height > 1024 * 1024 or width % 64 or height % 64:
            raise ValueError("size must be multiples of 64 from 512 to 1024, at most 1024² pixels")
        return value


class AppliedLora(BaseModel):
    lora_id: str
    scale: float
    sha256: str


class GenerationMetadata(BaseModel):
    request_id: str
    model_revision: str
    seed: int
    loras: list[AppliedLora]
    inference_ms: int


class GenerationData(BaseModel):
    b64_json: str


class GenerationResponse(BaseModel):
    created: int
    data: list[GenerationData]
    metadata: GenerationMetadata


class LifecycleResponse(BaseModel):
    model_id: str
    state: Literal["ready", "unloaded"]
    revision: str | None = None
