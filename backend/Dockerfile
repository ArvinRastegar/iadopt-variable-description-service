FROM python:3.12-slim

WORKDIR /app

# Useful environment defaults
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install Python dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code and data
RUN useradd --create-home --uid 10001 appuser
RUN mkdir -p /app/state && chown appuser:appuser /app/state
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser data ./data

USER appuser

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
