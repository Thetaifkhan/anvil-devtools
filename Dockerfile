FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as an unprivileged user instead of root.
RUN useradd --system --uid 10001 --no-create-home app && chown -R app /app
USER app

# Default to a single gunicorn worker so the in-process delivery worker
# (when SUPABASE_WORKER_ENABLED=1) is single-instance and doesn't compete
# on the shared SQLite outbox. For higher throughput scale horizontally
# (more containers) rather than vertically (more workers per container).
#
# No --preload. The delivery worker is a thread, and threads do not survive
# fork(). With preload, the thread started in the master would be lost in the
# child, leaving delivery silently disabled. Each worker imports the app and
# starts its own worker thread.
ENV PORT=8080 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=2)" || exit 1

# Default: 1 worker. Override WEB_CONCURRENCY to scale horizontally
# (note: more workers means more in-process delivery worker threads
# competing on the same SQLite outbox; the recommended production setup
# is SUPABASE_WORKER_ENABLED=0 on the web service + a dedicated
# Background Worker running `python -m delivery.worker_cli`).
CMD ["sh", "-c", "gunicorn -w ${WEB_CONCURRENCY:-1} -b 0.0.0.0:${PORT} app:app"]
