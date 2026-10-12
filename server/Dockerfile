ARG FRONTEND_BASE=node:24.18.1-bookworm-slim
ARG SERVER_BASE=python:3.12-slim
FROM ghcr.io/astral-sh/uv:0.12.3 AS uv
FROM ${FRONTEND_BASE} AS frontend
WORKDIR /app/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/index.html ui/vite.config.js ./
COPY ui/src ./src
RUN npm run build
FROM ${SERVER_BASE}
USER root
ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision=${VCS_REF}
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app/server
ENV PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
COPY server/pyproject.toml server/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY server/platform_service ./platform_service
COPY server/tests ./tests
COPY --from=frontend /app/ui/dist /app/ui/dist
COPY design/model-config.schema.json /app/design/model-config.schema.json
RUN mkdir -p /data && chown -R 1000:1000 /data
USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready',timeout=2)"
CMD ["uvicorn","platform_service.app:create_app","--factory","--host","0.0.0.0","--port","8000","--workers","1"]
