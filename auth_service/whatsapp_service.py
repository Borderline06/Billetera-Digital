"""
Servicio para enviar mensajes de verificación vía WhatsApp (usando n8n).
"""

import httpx
import logging
from utils import N8N_VERIFICATION_WEBHOOK_URL # Importamos la URL desde utils

logger = logging.getLogger(__name__)

async def send_verification_code(phone_number: str, code: str) -> bool:
    """
    Envía una solicitud POST asíncrona al webhook de n8n para disparar el envío de WhatsApp.
    
    Args:
        phone_number (str): El número de teléfono del destinatario.
        code (str): El código de 6 dígitos a enviar.

    Returns:
        bool: True si el webhook fue invocado exitosamente (Respuesta 2xx), False en caso contrario.
    """
    if not N8N_VERIFICATION_WEBHOOK_URL:
        logger.error("No se puede enviar el código: N8N_VERIFICATION_WEBHOOK_URL no está configurada.")
        return False

    # El payload que esperamos que n8n reciba (ajusta si es necesario)
    payload = {
        "phone": phone_number,
        "code": code,
        "message": f"Tu código de verificación para Pixel Money es: {code}"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(N8N_VERIFICATION_WEBHOOK_URL, json=payload, timeout=10.0)
            
            response.raise_for_status() # Lanza error si la respuesta es 4xx o 5xx
            
            logger.info(f"Webhook de n8n invocado exitosamente para {phone_number}")
            return True

    except httpx.HTTPStatusError as e:
        logger.error(f"Error al invocar n8n (HTTP Status): {e.response.status_code} - {e.response.text}")
        return False
    except httpx.RequestError as e:
        logger.error(f"Error de red o conexión al invocar n8n: {e}")
        return False
    except Exception as e:
        logger.error(f"Error inesperado al enviar código a n8n: {e}", exc_info=True)
        return False