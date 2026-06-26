FROM python:3.13.4-slim

# Install Rust compiler
RUN apt-get update && apt-get install -y \
    build-essential \
    cargo \
    rustc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Run the main application
CMD ["python", "main.py"]

