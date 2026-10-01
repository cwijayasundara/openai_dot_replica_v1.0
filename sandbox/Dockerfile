# The dot's computer. Build: docker compose build sandbox
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ripgrep curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pandas duckdb openpyxl jinja2

RUN useradd --create-home --uid 1000 runner \
 && mkdir -p /in /skills /work \
 && chown runner:runner /work

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER runner
WORKDIR /work
CMD ["sleep", "infinity"]
