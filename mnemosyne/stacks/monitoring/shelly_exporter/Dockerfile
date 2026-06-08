FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir flask prometheus_client requests

COPY exporter.py .

EXPOSE 9117
CMD ["python", "exporter.py"]