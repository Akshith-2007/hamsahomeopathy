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

# No USER here: the entrypoint fixes ownership of the uploads disk as root, then drops to "app".
EXPOSE 5000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
# Listens on $PORT when the host sets it (Render), otherwise 5000.
CMD ["sh", "-c", "exec gunicorn hamsa.wsgi:application --bind 0.0.0.0:${PORT:-5000} --workers ${WEB_CONCURRENCY:-3} --timeout 120 --access-logfile -"]
