FROM ghcr.io/astral-sh/uv:0.12.3 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app/server
ENV PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
COPY server/pyproject.toml server/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY server/platform_service ./platform_service
COPY server/tests ./tests
COPY ui /app/ui
COPY design/model-config.schema.json /app/design/model-config.schema.json
RUN mkdir /data && chown -R 1000:1000 /data
USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready',timeout=2)"
CMD ["uvicorn","platform_service.app:create_app","--factory","--host","0.0.0.0","--port","8000","--workers","1"]
