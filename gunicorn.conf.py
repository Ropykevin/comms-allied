import multiprocessing
import os

bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")
workers = int(os.environ.get("GUNICORN_WORKERS", min(multiprocessing.cpu_count() * 2 + 1, 8)))
threads = int(os.environ.get("GUNICORN_THREADS", "2"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
graceful_timeout = 30
keepalive = 5

# Recycle workers periodically to contain slow memory growth.
max_requests = 1000
max_requests_jitter = 100

# Docker's overlay filesystem makes the default heartbeat directory slow.
worker_tmp_dir = "/dev/shm"

accesslog = "-"
# %(U)s is the path without the query string, so tokens and search terms are not logged.
access_log_format = '%(h)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(L)ss'
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
