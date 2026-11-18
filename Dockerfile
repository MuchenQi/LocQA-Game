# locqa web — one image for both processes.
#   web:     the default CMD
#   worker:  docker run … locqa-worker
# On a single free-tier service, leave LOCQA_INLINE_WORKER=1 and run only the
# web process; the worker loop runs as a thread inside it.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LOCQA_SAMPLES_DIR=/app/samples \
    PORT=8000

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY locqa ./locqa
COPY locqa_web ./locqa_web
RUN pip install ".[web]"

COPY samples ./samples

RUN useradd --create-home --uid 10001 locqa
USER locqa

EXPOSE 8000
# --proxy-headers: platforms terminate TLS in front of the container.
CMD ["sh", "-c", "exec uvicorn locqa_web.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
