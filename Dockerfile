# Multi-stage production Dockerfile for Operatives Monitoring System
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv

# Copy project definition
COPY pyproject.toml README.md ./

# Install python dependencies into system environment
RUN uv pip install --system -e .

# Install Playwright browser dependencies (for headless social scraping)
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy application source
COPY src/ /app/src/
COPY scripts/ /app/scripts/
COPY alembic.ini /app/alembic.ini

# Create session directory for persistent userbot session files
RUN mkdir -p /app/sessions

CMD ["python", "src/main.py"]
