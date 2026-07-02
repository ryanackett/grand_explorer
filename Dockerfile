# Use a clean, small Python image
FROM python:3.11-slim

# Prevent Python from writing .pyc files and keep logs coming in real-time
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

# 1. Install system dependencies for Postgres and Images
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    postgresql-client \
    default-libmysqlclient-dev \
    pkg-config \
    git \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Python dependencies
# We copy this separately so Docker "caches" your layers (faster builds)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copy your actual code into /app
COPY . .

# 4. Start the engine.
# migrate + collectstatic run at container start (not build) so they use the
# real runtime env vars (SECRET_KEY, DATABASE_URL). Gunicorn binds Railway's
# injected $PORT (falls back to 8000 locally). --timeout 120 gives large cart
# ZIP exports room to stream without gunicorn killing the worker.
CMD python manage.py migrate --noinput && \
    python manage.py collectstatic --noinput && \
    gunicorn fluxapp.wsgi --bind 0.0.0.0:${PORT:-8000} --workers 2 --timeout 120