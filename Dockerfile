# Phase 1 application image.
# Single-stage build — the dependency set is small and this image is for a
# local demo, not production distribution.
FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for better layer caching. psycopg[binary] ships
# its own libpq, so no system postgres-client package is required.
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

# Copy the application source and migration tooling.
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# uvicorn serves the FastAPI app; --host 0.0.0.0 makes it reachable from the host.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
