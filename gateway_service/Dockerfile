FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Exponemos el puerto 8080 (el puerto público)
EXPOSE 8080

# Usaremos main.py como nuestro archivo principal
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--reload"]