FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MEDIA_ROOT=/app/media \
    STATIC_ROOT=/app/staticfiles

WORKDIR /app

# Wheels for psycopg[binary] and Pillow bundle their native libraries.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN sed -i 's/\r$//' docker/entrypoint.sh && chmod +x docker/entrypoint.sh \
 && DJANGO_SECRET_KEY=collectstatic-only DB_ENGINE=sqlite python manage.py collectstatic --noinput \
 && useradd --uid 1000 --create-home app \
 && mkdir -p /app/media && chown -R app:app /app/media

# The app always runs as the unprivileged "app" user (uid 1000). The entrypoint starts as root only to
# hand a freshly mounted uploads disk to that user, then drops privileges before anything else runs.
# Kubernetes starts the container as uid 1000 directly (see k8s/30-web-deployment.yaml).
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '5000'), timeout=4)" || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
# Port, workers and timeouts: docker/gunicorn.conf.py (reads $PORT, $WEB_CONCURRENCY, $GUNICORN_TIMEOUT).
CMD ["gunicorn", "--config", "docker/gunicorn.conf.py", "hamsa.wsgi:application"]
