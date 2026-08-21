# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Non-root: the app never needs privileges, and a container that runs as root
# gives an attacker one less step.
RUN useradd --create-home --uid 10001 ward

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Scans and (by default) the SQLite file live here; mount a volume over it.
RUN mkdir -p /data/uploads && chown -R ward:ward /data /app
USER ward

ENV WARD_ENV=production \
    WARD_UPLOAD_DIR=/data/uploads \
    WARD_DATABASE_URL=sqlite:////data/ward.db

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz').read()"

# One worker on purpose. The audit log is a linear hash chain and the login
# throttle is in-process; both assume a single writer. Scale with more
# containers behind a load balancer only after moving to Postgres and taking a
# lock around audit appends — see DEPLOY.md.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
