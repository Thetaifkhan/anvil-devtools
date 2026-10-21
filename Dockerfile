FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as an unprivileged user instead of root.
RUN useradd --system --uid 10001 --no-create-home app && chown -R app /app
USER app

ENV PORT=8080 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=2)" || exit 1

# gunicorn with 4 workers is a sane default for a small-to-medium deployment;
# tune --workers based on available CPU cores.
CMD ["sh", "-c", "gunicorn -w 4 -b 0.0.0.0:${PORT} app:app"]
