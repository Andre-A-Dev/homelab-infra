FROM python:3.12-slim-bookworm

# WeasyPrint needs Pango/Harfbuzz; Poppler provides pdftoppm + pdftotext
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libffi8 \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /srv/carousel
COPY app/ app/
COPY templates_ig/ templates_ig/
COPY scripts/ scripts/

RUN mkdir -p /data/jobs
ENV PYTHONUNBUFFERED=1

EXPOSE 5000
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health', timeout=3)"

# gthread: an idle or slow connection (browser preconnect, status polling,
# many slide images at once) blocks only one thread, not a whole worker.
# Rendering itself runs in separate job processes, not in the web workers.
CMD ["gunicorn", "--chdir", "app", "--bind", "0.0.0.0:5000", \
     "--workers", "2", "--worker-class", "gthread", "--threads", "4", \
     "--timeout", "120", "app:app"]
