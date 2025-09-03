# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}

# Basic runtime hygiene
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps (if you later need libpq etc., add here)
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps early for better layer caching
COPY requirements.txt ./
RUN pip install -r requirements.txt && \
    python -c "import pkgutil,sys; sys.exit(0 if pkgutil.find_loader('gunicorn') else 1)" || pip install gunicorn

# App source
COPY . .

# Non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# Data dir (mounted as a volume at runtime)
ENV DATA_DIR=/data
RUN mkdir -p ${DATA_DIR}/uploads

# Web port (Render sets PORT; locally we keep 5050)
ENV PORT=5050
EXPOSE 5050

# Start with Gunicorn (threads model keeps footprint small)
CMD ["bash", "-lc", "exec gunicorn -w 2 -k gthread --threads 8 --timeout 90 --bind 0.0.0.0:${PORT} app:app"]
