# monitoring/Dockerfile
FROM python:3.11-slim
WORKDIR /app
# Instalamos las librerías que usa watchdog.py
RUN pip install --no-cache-dir docker requests 
COPY watchdog.py .
# Comando para ejecutar el script al iniciar el contenedor
CMD ["python", "-u", "watchdog.py"] 
# El -u es para que los prints() salgan inmediatamente en los logs