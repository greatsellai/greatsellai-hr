FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

ARG DEBIAN_MIRROR=mirrors.cloud.tencent.com
RUN sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        libreoffice-calc \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements-production.lock ./

ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
RUN pip install --upgrade --no-cache-dir --index-url ${PIP_INDEX_URL} \
        -r requirements-production.lock \
    && pip check

COPY app ./app
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

# Dependencies above are a reviewed, exact production lock. Installing the
# application without dependency resolution makes a later image rebuild reuse
# that exact set rather than silently selecting newer package releases.
RUN pip install --no-deps --no-build-isolation . \
    && pip check

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /var/lib/resume-v3 \
    && chown -R appuser:appuser /app /var/lib/resume-v3

USER appuser
EXPOSE 8000

# Caddy owns public access logging and removes raw request targets. The API
# process must not emit Uvicorn's default access lines, which include query
# strings such as OAuth or password-reset tokens.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
