# midea_exporter/Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Pin a known-good version — the authenticate() signature and
# device attributes have shifted across releases.
RUN pip install --no-cache-dir msmart-ng==2026.4.1 prometheus_client

COPY midea_exporter.py .

EXPOSE 9115
CMD ["python", "midea_exporter.py"]