# Use official Python runtime as base image
FROM python:3.11-slim

# Install system dependencies required for PDF processing
RUN apt-get update && apt-get install -y \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app
ENV SESSION_DATA_DIR=/app/data REQUIRE_PERSISTENT_STORAGE=true

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directories
RUN mkdir -p data/input data/extracted data/output

# Expose Streamlit port
EXPOSE 8501

# Health check
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:' + __import__('os').getenv('PORT', '8501') + '/healthz', timeout=5).read()" || exit 1

# Run Streamlit app
CMD ["python", "serve.py"]
