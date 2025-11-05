"""Modelos Pydantic (schemas) para validación de datos de entrada/salida en el Servicio de Autenticación."""

from pydantic import BaseModel, EmailStr, Field, ConfigDict
from typing import Optional

# --- Schemas de Usuario ---

class UserCreate(BaseModel):
    """Schema para los datos requeridos al crear un nuevo usuario."""
    email: str
    password: str = Field(..., min_length=8, description="La contraseña debe tener al menos 8 caracteres")

class UserResponse(BaseModel):
    """Schema para los datos devueltos tras la creación exitosa de un usuario (excluye contraseña)."""
    id: int
    email: str

    # Configuración de Pydantic v2+ para permitir mapeo desde modelos ORM (SQLAlchemy)
    model_config = ConfigDict(from_attributes=True)


# --- Schemas de Token ---

class Token(BaseModel):
    """Schema para el token de acceso JWT devuelto tras un login exitoso."""
    access_token: str
    token_type: str = "bearer" # Valor por defecto 'bearer' según estándar OAuth2

class TokenPayload(BaseModel):
    """Schema que representa el payload decodificado de un token JWT válido."""
    # 'sub' (subject) típicamente contiene el identificador del usuario. Usamos user_id como string.
    sub: Optional[str] = None
    exp: Optional[int] = None # 'exp' (expiration time) es una marca de tiempo Unix