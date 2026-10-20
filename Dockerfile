FROM python:3.12-slim

# streamlink is deliberately not pinned: YouTube changes often and only recent
# streamlink versions keep up. Rebuild with `docker compose build --pull --no-cache`
# to get the latest one.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir streamlink \
 && useradd --system --no-create-home restreamer

WORKDIR /app
COPY restream.py .

USER restreamer
ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    ON_DEMAND=1
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/status', timeout=4)"

CMD ["python", "restream.py"]
