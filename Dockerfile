# Build dependencies in a separate stage so compilers and headers do not ship in
# the runtime image. Python 3.12 matches the CI environment.
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

# mysqlclient needs these only while its wheel is built.
RUN apt-get update && apt-get install -y \
    gcc \
    default-libmysqlclient-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Runtime libraries only; compilers, headers, git, and database clients stay in
# the builder image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmariadb3 \
    libjpeg62-turbo \
    zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin appuser

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels /wheels/* \
    && rm -rf /wheels

COPY . .
RUN chown -R appuser:appuser /app
USER appuser

# Static files must be collected in the serving container because Railway's
# pre-deploy container filesystem is not shared with the runtime container.
# Gunicorn binds Railway's injected $PORT (falls back to 8000 locally).
CMD python manage.py collectstatic --noinput && \
    gunicorn fluxapp.wsgi --bind 0.0.0.0:${PORT:-8000} --workers 2 --timeout 120
