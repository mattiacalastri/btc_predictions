FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["sh", "-c", "gunicorn --timeout 300 --graceful-timeout 30 --bind 0.0.0.0:${PORT:-8080} app:app"]
