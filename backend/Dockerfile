# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.lock \
    && groupadd --gid 10001 tripai \
    && useradd --uid 10001 --gid tripai --create-home tripai
COPY --chown=tripai:tripai app ./app
COPY --chown=tripai:tripai scripts ./scripts
COPY --chown=tripai:tripai samples ./samples
RUN mkdir -p runtime/logs && chown -R tripai:tripai runtime
USER tripai
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"
# One worker preserves the process-local SSE bus and background planning tasks.
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "30"]
