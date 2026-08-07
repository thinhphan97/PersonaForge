FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROJECT_ROOT=/app

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY config ./config
COPY frontend ./frontend
COPY scripts ./scripts
RUN bash scripts/generate_demo_videos.sh

EXPOSE 8000
CMD ["uvicorn", "personaforge_chat.main:app", "--host", "0.0.0.0", "--port", "8000"]
