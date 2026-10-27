FROM python:3.12-slim

# Sin .pyc y logs sin buffer: así `docker logs` muestra todo al instante.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge.py .

# Mismo UID/GID que el resto del stack: /data queda escribible sin root.
USER 1000:1000

EXPOSE 8080
CMD ["python", "bridge.py"]
