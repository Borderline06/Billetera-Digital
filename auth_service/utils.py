"""Funciones de utilidad para el servicio de autenticación, incluyendo hash de contraseñas y manejo de JWT."""

import os
import logging
import bcrypt
import random # <-- NUEVO
import string # <-- NUEVO
import httpx # <-- NUEVO
from datetime import datetime, timedelta, timezone
from passlib.context import CryptContext
from jose import JWTError, jwt
from dotenv import load_dotenv
from typing import Dict, Optional
from db import get_db
from fastapi import HTTPException # <-- NUEVO

# Carga variables de entorno desde .env
load_dotenv()

# Configuración del logger
logger = logging.getLogger(__name__)

# --- Configuración de Seguridad ---
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    logger.warning("JWT_SECRET_KEY no está definida en las variables de entorno. Usando clave insegura por defecto para desarrollo.")
    
    SECRET_KEY = "clave_secreta_insegura_por_defecto_cambiar_urgentemente" 

ALGORITHM = "HS256"

ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60 * 24))

# --- NUEVO: Configuración de Verificación y Telegram ---
VERIFICATION_CODE_EXPIRATION_MINUTES = int(os.getenv("VERIFICATION_CODE_EXPIRATION_MINUTES", 10))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "AQUI_VA_EL_TOKEN_DE_TU_BOT_DE_TELEGRAM":
    logger.error("TELEGRAM_BOT_TOKEN no está definido en .env. El envío de códigos de verificación fallará.")


pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    
)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica una contraseña plana contra un hash almacenado."""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Genera el hash de una contraseña plana usando bcrypt."""
    return pwd_context.hash(password)

# --- Utilidades para Tokens JWT ---
def create_access_token(data: Dict) -> str:
    """
    Genera un token de acceso JWT con los datos proporcionados y una marca de tiempo de expiración.

    Args:
        data: Diccionario (payload) a incluir en el token (ej., {'sub': user_id}).

    Returns:
        String del JWT codificado.
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def decode_token(token: str) -> Optional[Dict]:
    """
    Decodifica y valida un token JWT.

    Args:
        token: El string JWT a decodificar.

    Returns:
        El diccionario del payload decodificado si el token es válido y no ha expirado,
        en caso contrario, None.
    """
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"verify_aud": False} 
        )
       
        if payload.get("exp") and datetime.now(timezone.utc) < datetime.fromtimestamp(payload["exp"], tz=timezone.utc):
             return payload
        else:
            logger.warning("Fallo en decodificación de token: El token ha expirado.")
            return None
    except JWTError as e:
        logger.warning(f"Fallo en decodificación de token: {e}")
        return None
    except Exception as e: # Captura cualquier otro error inesperado
        logger.error(f"Error inesperado durante decodificación de token: {e}", exc_info=True)
        return None

# --- Service Discovery ---
# URL interna para el Balance Service
BALANCE_SERVICE_URL = os.getenv("BALANCE_SERVICE_URL")
if not BALANCE_SERVICE_URL:
     logger.error("Variable de entorno BALANCE_SERVICE_URL no está definida.")
     
# --- NUEVAS FUNCIONES DE VERIFICACIÓN ---

def generate_verification_code(length: int = 6) -> str:
    """Genera un código numérico aleatorio de 6 dígitos."""
    return "".join(random.choices(string.digits, k=length))

async def send_telegram_message(chat_id: str, message: str) -> bool:
    """
    Envía un mensaje a un chat_id de Telegram usando httpx.
    Retorna True si fue exitoso, False si falló.
    """
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "AQUI_VA_EL_TOKEN_DE_TU_BOT_DE_TELEGRAM":
        logger.error("No se puede enviar mensaje: TELEGRAM_BOT_TOKEN no configurado.")
        return False

    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown" # Permite usar `*bold*` y `_italic_`
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            logger.info(f"Mensaje de Telegram enviado exitosamente a chat_id: {chat_id}")
            return True
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.error(f"Error al enviar mensaje de Telegram a {chat_id}: {exc}")
            if isinstance(exc, httpx.HTTPStatusError):
                logger.error(f"Respuesta de Telegram: {exc.response.text}")
            return False