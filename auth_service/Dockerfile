# 1. Usamos una imagen base oficial de Python (ligera)
FROM python:3.11-slim

# 2. Establecemos el directorio de trabajo dentro del contenedor
WORKDIR /app

# 3. Copiamos SOLO la lista de requisitos e instalamos dependencias
# Esto aprovecha el caché de Docker: si no cambiamos los requirements,
# no se reinstalará todo cada vez que cambiemos el código.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copiamos el resto de nuestro código de Python
COPY . .

# 5. Exponemos el puerto INTERNO en el que correrá FastAPI
# (Coincide con el "target" del docker-compose)
EXPOSE 8000

# 6. El comando para arrancar el servidor de FastAPI
# --host 0.0.0.0 hace que sea accesible desde fuera del contenedor
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]