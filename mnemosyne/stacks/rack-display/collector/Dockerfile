FROM python:3.13-slim

# No Pillow, no fonts, no contrib repo — the client owns every pixel now.
RUN pip install --no-cache-dir pyyaml \
 && apt-get update && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 9119
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD curl -fsS http://127.0.0.1:9119/healthz || exit 1

CMD ["python3", "-u", "app.py"]
