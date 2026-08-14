# Small, single-purpose exporter image. Pin the base by digest in production if
# you want fully reproducible builds (fits your deliberate-update philosophy).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY prusa_exporter.py .

# Drop privileges: the exporter only makes outbound HTTP calls and serves /metrics.
USER nobody

EXPOSE 9118

ENTRYPOINT ["python", "-u", "prusa_exporter.py"]
