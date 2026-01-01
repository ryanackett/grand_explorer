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

# 4. Prepare static files (Critical for CSS/Images to work)
# This gathers everything into the 'staticfiles' folder we defined in settings.py
#RUN python manage.py collectstatic --noinput

# 5. Start the engine
#CMD gunicorn blog.wsgi --bind 0.0.0.0:8000