FROM python:3.13-slim-bookworm

# Install uv
COPY --from=astral/uv:latest /uv /bin/uv

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies from the lockfile (no dev deps, no editable installs)
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code
COPY . .

# Make the venv available on PATH
ENV PATH="/app/.venv/bin:$PATH"

RUN useradd -r appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]

CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "app:app"]
