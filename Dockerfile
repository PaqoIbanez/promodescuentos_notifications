# Build stage
FROM python:3.11-slim AS builder

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies to a virtual environment or usage --user
# Here we use --user for simplicity in copying, or install to /install
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Runner stage
FROM python:3.11-slim AS runner

WORKDIR /app

# Create a non-root user
RUN useradd -m -u 1000 appuser

# Install runtime dependencies (libpq for psycopg2) & curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed python packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY . .

# Set ownership to appuser
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose port (default 10000)
ENV PORT=10000
EXPOSE $PORT

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:$PORT/health || exit 1

# Command to run the application (JSON array format for signal handling)
CMD ["sh", "-c", "python init_db.py && uvicorn app.main:app --host 0.0.0.0 --port $PORT"]