FROM python:3.12-alpine

WORKDIR /app
COPY server.py portal.html ./
COPY static ./static

RUN mkdir -p /data
ENV LISTEN=0.0.0.0:8080 \
    LOG_PATH=/data/access.log \
    PYTHONUNBUFFERED=1

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD wget -q -O - http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "server.py"]
