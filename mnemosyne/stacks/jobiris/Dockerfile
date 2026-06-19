FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code (job-monitor.py, board.py, search-profiles.yaml) is
# mounted as a volume at runtime - see docker-compose.yml. This keeps the
# image stable across code/config changes; only requirements.txt edits
# require a rebuild.

CMD ["python3", "board.py"]
