# Multi-arch image (amd64 + arm64, ADR 0015): a slim base keeps both small and
# there are no compiled wheels to cross-build.
FROM python:3.15-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

# Pull in Debian security fixes the floating base tag has not caught up with yet
# (Trivy gate, demo #112: zlib1g HIGH CVEs with a fix available). Scoped to the
# one package so builds stay predictable; delete this layer once the upstream
# python image ships the fix. Runs as root, before USER, and leaves no apt lists.
# Re-tested 2026-11-17 on python:3.15-slim (Debian 13.7): still needed, the scan
# fails on zlib1g (CVE-2026-85091) without it (demo #187).
RUN apt-get update     && apt-get install -y --no-install-recommends --only-upgrade zlib1g     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# pip is removed in the SAME layer as the install, so no layer keeps it. The
# base image's pip vendors its own copies of msgpack, setuptools and urllib3,
# and Trivy fails the build on their HIGH CVEs (demo #187). Upgrading pip is not
# enough: even pip 26.3 still vendors setuptools 70.3.0. The app never runs pip,
# so removing it is safe and also shrinks the image's attack surface. If the
# uninstall ever fails, the build stops: a loud failure, never a silent one.
RUN pip install -r requirements.txt && pip uninstall -y pip

COPY app ./app

# Non-root with a numeric UID, so Kubernetes runAsNonRoot can verify it.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
USER 10001

# uvicorn reads any of its options from UVICORN_* variables (UVICORN_PORT,
# UVICORN_LOG_LEVEL, ...), so `docker run -e UVICORN_PORT=8080` changes the
# port. A command-line flag would override the variable, so none is passed.
ENV UVICORN_HOST=0.0.0.0 UVICORN_PORT=8000

# 8000: the app (default). 9000: Prometheus metrics, kept off the public Service.
EXPOSE 8000 9000
CMD ["uvicorn", "app.main:app"]
