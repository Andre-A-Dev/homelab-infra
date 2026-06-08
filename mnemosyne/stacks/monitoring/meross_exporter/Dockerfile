FROM python:3.11-alpine

WORKDIR /app

RUN apk add --no-cache gcc musl-dev libffi-dev openssl-dev

RUN pip install --no-cache-dir \
    meross-iot==0.4.10.4 \
    prometheus-client==0.20.0

RUN apk del gcc musl-dev

COPY exporter.py .

CMD ["python", "-u", "exporter.py"]