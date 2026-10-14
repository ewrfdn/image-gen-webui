# CUDA kernels come from the pinned cu130 PyTorch wheel, including GB10 support.
ARG CUDA_BASE=nvidia/cuda:13.3.1-cudnn-runtime-ubuntu24.04
FROM ghcr.io/astral-sh/uv:0.12.3 AS uv
FROM ${CUDA_BASE} AS runtime
COPY --from=uv /uv /uvx /usr/local/bin/
ARG SERVICE_VERSION=0.1.0
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="image-gen-inference" \
      org.opencontainers.image.version=${SERVICE_VERSION} \
      org.opencontainers.image.revision=${VCS_REF}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH HF_HUB_OFFLINE=1 HF_HOME=/tmp/huggingface
RUN uv python install 3.14.6 && uv venv --python 3.14.6 /opt/venv
RUN uv pip install --python /opt/venv/bin/python torch==2.9.1+cu130 \
    --index-url https://download.pytorch.org/whl/cu130
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Use the lock's non-Torch dependencies; never replace the CUDA wheel with gpu extra's Torch.
RUN uv export --frozen --extra gpu --extra test --no-emit-project --no-emit-package torch \
    --no-hashes --output-file /tmp/requirements.txt && \
    uv pip install --python /opt/venv/bin/python --no-deps -r /tmp/requirements.txt
# Qwen3VL's processor imports its video processor even for text-only generation.
RUN uv pip install --python /opt/venv/bin/python --no-deps torchvision==0.24.1 \
    --index-url https://download.pytorch.org/whl/cu130
COPY inference_service ./inference_service
COPY scripts ./scripts
COPY tests ./tests
RUN uv pip install --python /opt/venv/bin/python --no-deps . && \
    python -c "import torch; from diffusers import QwenImage21Pipeline; print(torch.__version__)" && \
    uv pip freeze --python /opt/venv/bin/python > /app/runtime-requirements.txt
USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4)"
CMD ["uvicorn", "inference_service.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
FROM runtime AS dev
CMD ["uvicorn", "inference_service.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--reload", "--reload-dir", "/app/inference_service"]
