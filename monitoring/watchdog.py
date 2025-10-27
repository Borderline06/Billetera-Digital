# monitoring/watchdog.py
import docker
import requests
import time
import os
import logging

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')

# Leemos la URL del webhook de n8n desde las variables de entorno
N8N_ALERT_WEBHOOK = os.getenv("N8N_ALERT_WEBHOOK", "http://n8n:5678/webhook/recovery")
# Intervalo de chequeo en segundos
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
# Lista de contenedores CLAVE a monitorear
MONITORED_CONTAINERS = ["gateway_service", "auth_service", "balance_service", "ledger_service", "n8n"]

try:
    # Intenta conectarse al daemon de Docker a través del socket montado
    client = docker.from_env()
except Exception as e:
    logging.critical(f"❌ FATAL: No se pudo conectar al daemon de Docker. ¿Está montado el socket /var/run/docker.sock? Error: {e}")
    # El script no puede funcionar sin acceso a Docker, salimos.
    exit(1)

def check_containers():
    logging.info("🩺 Iniciando ciclo de verificación de contenedores...")
    for container_name in MONITORED_CONTAINERS:
        try:
            container = client.containers.get(container_name)
            # Obtenemos el estado de salud DEL CONTENEDOR (si tiene healthcheck)
            # Puede ser 'starting', 'healthy', 'unhealthy', o None si no tiene healthcheck
            health_status = container.attrs.get("State", {}).get("Health", {}).get("Status")
            container_status = container.status # 'running', 'exited', etc.

            # Consideramos el contenedor "caído" si no está corriendo o está marcado como 'unhealthy'
            if container_status != "running" or health_status == "unhealthy":
                logging.warning(f"⚠️ Contenedor '{container_name}' detectado como {container_status}/({health_status or 'no healthcheck'}). Intentando reiniciar...")
                try:
                    container.restart(timeout=30) # Intentamos reiniciar con 30s de espera
                    logging.info(f"✅ Contenedor '{container_name}' reiniciado.")
                    send_alert(container_name, "reiniciado_por_watchdog", health_status or container_status)
                except Exception as restart_err:
                    logging.error(f"❌ Error al intentar reiniciar '{container_name}': {restart_err}")
                    send_alert(container_name, "fallo_reinicio_watchdog", str(restart_err))
            # else: # Descomentar para logs más verbosos
            #    logging.info(f"✔️ Contenedor '{container_name}' está {container_status}/({health_status or 'ok'}).")

        except docker.errors.NotFound:
            logging.error(f"❌ Contenedor '{container_name}' no encontrado. ¿Está definido en docker-compose?")
            send_alert(container_name, "no_encontrado", "docker.errors.NotFound")
        except Exception as e:
            logging.error(f"❓ Error inesperado al verificar '{container_name}': {e}")
            send_alert(container_name, "error_verificacion_watchdog", str(e))

def send_alert(container_name, action, detail="N/A"):
    payload = {
        "container": container_name,
        "action": action,
        "detail": detail,
        "timestamp": datetime.now().isoformat()
    }
    try:
        response = requests.post(N8N_ALERT_WEBHOOK, json=payload, timeout=10)
        response.raise_for_status() # Lanza error si n8n no responde OK
        logging.info(f"📨 Notificación enviada a n8n para '{container_name}' (Acción: {action})")
    except Exception as e:
        logging.error(f"🔥 Error al enviar notificación a n8n para '{container_name}': {e}")

if __name__ == "__main__":
    logging.info("--- [Watchdog Bank A] Iniciado ---")
    logging.info(f"Monitoreando contenedores: {', '.join(MONITORED_CONTAINERS)}")
    logging.info(f"Intervalo de chequeo: {CHECK_INTERVAL} segundos")
    logging.info(f"Notificando recuperaciones a: {N8N_ALERT_WEBHOOK}")

    while True:
        check_containers()
        logging.info(f"Durmiendo por {CHECK_INTERVAL} segundos...")
        time.sleep(CHECK_INTERVAL)