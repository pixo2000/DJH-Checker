FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    DATA_DIR=/data \
    TZ=Europe/Berlin

WORKDIR /app
COPY djh_checker.py .

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 djh \
    && mkdir -p /data && chown djh:djh /data
USER djh
VOLUME ["/data"]

CMD ["python", "djh_checker.py"]
