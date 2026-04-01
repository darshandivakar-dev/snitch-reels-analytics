FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY app.py .
COPY index.html .

# Expose port (Railway/Render inject $PORT at runtime)
EXPOSE 8000

# Start server
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
