FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
COPY vendor ./vendor
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV JOBPILOT_DB_PATH=/app/data/jobpilot.db
RUN mkdir -p /app/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
