# Whirlpool-Steuerung: Backend + Frontend in einem Image.
# Die ESP8266-Bridge-Firmware (firmware/) ist NICHT Teil dieses Images -
# die läuft auf dem ESP8266 selbst, nicht im Container.

FROM python:3.12-slim

WORKDIR /srv

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

WORKDIR /srv/backend

# Internal port is fixed at 8000; map it to whatever host port you want
# via docker-compose.yml (default in this project: 8050).
EXPOSE 8000

CMD ["python", "run.py"]
