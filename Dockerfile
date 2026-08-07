FROM python:3.11-slim

WORKDIR /srv
COPY pyproject.toml .
COPY app ./app
RUN pip install --no-cache-dir .

ENV DB_PATH=/data/tracker.db
RUN mkdir -p /data
EXPOSE 8080

# Shell form so Railway's injected $PORT expands; 8080 for plain docker runs.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
