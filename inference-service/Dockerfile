# CUDA kernels come from the pinned cu130 PyTorch wheel, including GB10 support.
ARG CUDA_BASE=nvidia/cuda:13.3.1-cudnn-runtime-ubuntu24.04
FROM ghcr.io/astral-sh/uv:0.12.3 AS uv
FROM ${CUDA_BASE} AS runtime
COPY --from=uv /uv /uvx /usr/local/bin/
ARG SERVICE_VERSION=0.1.0
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="image-gen-inference" \
      org.opencontainers.image.version="${SERVICE_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH HF_HUB_OFFLINE=1 HF_HOME=/tmp/huggingface
RUN uv python install 3.14.6 && uv venv --python 3.14.6 /opt/venv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
# The GPU stack is installed explicitly: the lock predates the MiniMax-H3-Image support.
RUN uv pip install --python /opt/venv/bin/python --no-deps \
      torch==2.13.0+cu130 torchvision==0.28.0+cu130 \
      --index-url https://download.pytorch.org/whl/cu130 && \
    uv export --frozen --extra test --no-emit-project --no-emit-package torch \
      --no-hashes --output-file /tmp/requirements.txt && \
    uv pip install --python /opt/venv/bin/python --no-deps -r /tmp/requirements.txt
# MiniMax-H3-Image lives in the modular pipeline that was merged on 2026-11-10; no
# Diffusers release on PyPI carries it yet, so the commit is pinned.
RUN uv pip install --python /opt/venv/bin/python --no-deps \
      "transformers==5.22.2" "accelerate==1.15.0" "safetensors==0.8.0" \
      "diffusers @ git+https://github.com/huggingface/diffusers@875373ac7f5856071e345a3e394c74efccc6b9e6"
COPY inference_service ./inference_service
COPY scripts ./scripts
COPY tests ./tests
RUN uv pip install --python /opt/venv/bin/python --no-deps . && \
    python -c "import torch, diffusers; \
from diffusers import MiniMaxH3ImageModularPipeline; \
print(torch.__version__, diffusers.__version__)" && \
    uv pip freeze --python /opt/venv/bin/python > /app/runtime-requirements.txt
USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4)"
CMD ["uvicorn", "inference_service.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
FROM runtime AS dev
CMD ["uvicorn", "inference_service.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--reload", "--reload-dir", "/app/inference_service"]
