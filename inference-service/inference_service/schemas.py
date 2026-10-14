from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoraSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lora_id: str = Field(min_length=1, max_length=128)
    scale: float = Field(default=1.0, ge=0.0, le=2.0, allow_inf_nan=False)


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=128)
    expected_revision: str | None = Field(default=None, min_length=1, max_length=128)
    expected_artifact_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    expected_lora_sha256: dict[str, str] = Field(default_factory=dict)
    prompt: str = Field(min_length=1, max_length=4000)
    size: str = "1024x1024"
    n: int = Field(default=1, ge=1, le=8, strict=True)
    image: list[str] = Field(default_factory=list, max_length=8)
    response_format: Literal["b64_json"] = "b64_json"
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    num_inference_steps: int | None = Field(default=None, ge=1, le=100)
    guidance_scale: float | None = Field(default=None, ge=0, le=20, allow_inf_nan=False)
    negative_prompt: str | None = Field(default=None, max_length=4000)
    loras: list[LoraSelection] = Field(default_factory=list, max_length=8)

    @field_validator("loras")
    @classmethod
    def validate_loras(cls, values):
        if len({value.lora_id for value in values}) != len(values):
            raise ValueError("duplicate LoRA selection")
        return values

    @field_validator("image")
    @classmethod
    def validate_images(cls, values):
        import base64
        for value in values:
            if len(value) > 28 * 1024 * 1024:
                raise ValueError("image exceeds base64 limit")
            try:
                base64.b64decode(value, validate=True)
            except ValueError:
                raise ValueError("image must contain base64, not URLs") from None
        return values

    @field_validator("size")
    @classmethod
    def validate_size(cls, value: str) -> str:
        try:
            width, height = (int(part) for part in value.split("x"))
        except (ValueError, AttributeError):
            raise ValueError("size must be WIDTHxHEIGHT") from None
        if width < 512 or height < 512 or width > 2752 or height > 2752 or width * height > 4_718_592 or width % 32 or height % 32:
            raise ValueError("size must use multiples of 32 from 512 to 2752, at most 4718592 pixels")
        return value


class AppliedLora(BaseModel):
    lora_id: str
    scale: float
    sha256: str


class GenerationMetadata(BaseModel):
    request_id: str
    model_revision: str
    model_artifact_sha256: str | None = None
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
