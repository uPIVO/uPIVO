FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv
WORKDIR /workspace/backend
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/workspace/backend/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY . .
RUN useradd --create-home appuser && chown -R appuser:appuser /workspace
USER appuser
EXPOSE 8000
CMD ["python", "-m", "app.main"]
