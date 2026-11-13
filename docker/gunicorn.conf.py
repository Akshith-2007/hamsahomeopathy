"""Gunicorn settings for the container. Override with environment variables."""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
# Sized for a small VPS (1-2 vCPU, a few GB RAM): 2 workers x 2 threads handles a busy clinic.
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
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
# Only nginx on the private Docker network can reach this port.
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "*")


def when_ready(server):
    if not os.environ.get("DOMAIN", "").strip():
        server.log.warning(
            "IP MODE: serving plain HTTP without a domain. For testing only - do NOT enter real patient "
            "data until a domain is set with scripts/set_domain.sh and the site runs on HTTPS.")
