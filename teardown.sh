#!/bin/bash
# teardown.sh - Detiene y limpia el entorno completo

echo "Deteniendo servicios..."
docker-compose down -v --remove-orphans

echo "Eliminando volúmenes (borrando todos los datos)..."
docker volume prune -f

echo "Entorno completamente limpio."