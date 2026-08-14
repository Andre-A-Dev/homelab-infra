FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static
# templates/ and sensors.yaml are bind-mounted (see docker-compose.yml) so edits
# apply on `docker compose restart aether` without rebuilding the image.

EXPOSE 8050

# 2 workers, 4 threads: plenty for a single-user console, keeps concurrent
# Prometheus queries snappy.
CMD ["gunicorn", "--bind", "0.0.0.0:8050", "--workers", "2", "--threads", "4", "app:app"]
