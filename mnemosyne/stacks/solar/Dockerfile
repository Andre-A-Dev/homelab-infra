FROM python:3.12-slim
WORKDIR /app
RUN pip install requests prometheus_client urllib3
COPY exporter.py .
CMD ["python", "-u", "exporter.py"]
