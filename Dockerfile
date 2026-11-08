# Multi-stage production Dockerfile for Operatives Monitoring System
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
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

# Install Playwright browser and its OS system dependencies
RUN playwright install --with-deps chromium

# Copy application source
COPY src/ /app/src/
COPY scripts/ /app/scripts/
COPY SKILL.md /app/SKILL.md
COPY alembic.ini /app/alembic.ini

# Install standalone CLIs (/usr/local/bin/ops and /usr/local/bin/sheets)
RUN cp /app/scripts/ops /usr/local/bin/ops && \
    cp /app/scripts/sheets /usr/local/bin/sheets && \
    chmod +x /usr/local/bin/ops /usr/local/bin/sheets /app/scripts/sync_hermes.sh

# Create session directory for persistent userbot session files
RUN mkdir -p /app/sessions

CMD ["python", "src/main.py"]
