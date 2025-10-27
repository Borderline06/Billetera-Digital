#!/bin/bash
# deploy.sh - Despliega la infraestructura completa de la Billetera Digital Bank A

echo "Despliegue iniciado para Bank Pixel Money..."
set -e

# 1. Verificar Docker
if ! command -v docker &> /dev/null; then
  echo "Docker no está instalado. Instálalo y vuelve a intentar."
  exit 1
fi

# 2. Construir y levantar todos los servicios
echo "Construyendo imágenes (si es necesario)..."
docker-compose build

echo "Levantando servicios..."
docker-compose up -d

# 3. Verificar estado
echo "Verificando salud de contenedores..."
sleep 10
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo "Despliegue completado exitosamente."
echo "Accede a:"
echo "  - MailHog (Correos): http://localhost:8025"
echo "  - n8n Dashboard: http://localhost:5678 (user: admin, pass: admin)"
echo "  - Prometheus: http://localhost:9090"
echo "  - Grafana: http://localhost:3000 (user: admin, pass: admin)"
echo "  - Alertmanager: http://localhost:9093"
echo "  - API Gateway: http://localhost:8080 (Aún no funcional)"