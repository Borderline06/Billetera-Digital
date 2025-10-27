FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# El puerto interno del contenedor. docker-compose lo mapeará a 8002.
EXPOSE 8000

# Usaremos main.py como nuestro archivo principal
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]