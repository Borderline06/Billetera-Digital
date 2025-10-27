# auth_service/utils.py
import os
from datetime import datetime, timedelta, timezone
from passlib.context import CryptContext
from jose import JWTError, jwt
from dotenv import load_dotenv

load_dotenv()

# --- Configuración de Seguridad ---
# Clave secreta para firmar los tokens JWT (leída desde .env)
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
# Algoritmo de firma para JWT
ALGORITHM = "HS256"
# Tiempo de vida de un token de acceso (ej. 1 día)
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 

# --- Utilidades para Contraseñas ---
# Configura el esquema de hashing (bcrypt es el recomendado)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica si una contraseña plana coincide con una hasheada."""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Genera el hash de una contraseña."""
    return pwd_context.hash(password)

# --- Utilidades para Tokens JWT ---
def create_access_token(data: dict) -> str:
    """Crea un nuevo token JWT."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def decode_token(token: str) -> dict | None:
    """Decodifica y valida un token JWT. Devuelve el payload o None si es inválido."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None

# --- Dirección del Balance Service ---
BALANCE_SERVICE_URL = os.getenv("BALANCE_SERVICE_URL")