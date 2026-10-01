FROM python:3.12-slim AS base

# ffmpeg = recording, alsa-utils = arecord / aplay / alsaloop,
# fonts-dejavu-core = text in the share video
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg alsa-utils fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Non-root user in the "audio" group (needed to open /dev/snd/*).
RUN useradd --create-home --uid 1000 --groups audio app \
 && mkdir -p /recordings /data && chown app:app /recordings /data

COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    RECORDINGS_DIR=/recordings \
    DATA_DIR=/data \
    PORT=8080

# ---- tests: docker build --target test -t audio-recorder:test . && docker run --rm audio-recorder:test ----
FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
USER app
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

# ---- app ----
FROM base AS app
COPY docker-entrypoint.sh /usr/local/bin/
# The entrypoint starts as root, fixes folder owners, then runs the app as "app".
ENTRYPOINT ["docker-entrypoint.sh"]
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
