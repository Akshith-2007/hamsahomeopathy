"""Gunicorn settings for the container. Override with environment variables."""
import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
# Clinic traffic is light; 2-4 sync workers per container is plenty. Default: 2 x CPU + 1, capped at 4.
workers = int(os.environ.get("WEB_CONCURRENCY", min(multiprocessing.cpu_count() * 2 + 1, 4)))
threads = int(os.environ.get("GUNICORN_THREADS", "2"))
# Uploads of several scanned case sheets can take a while on slow clinic connections.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5
# Recycle workers now and then so a slow memory leak can never take the clinic down.
max_requests = 1000
max_requests_jitter = 100
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "*")
