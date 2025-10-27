# auth_service/schemas.py
from pydantic import BaseModel, EmailStr

# --- Modelo para la creación de usuario (lo que esperamos en /register) ---
class UserCreate(BaseModel):
    email: EmailStr
    password: str

# --- Modelo para la respuesta de usuario (lo que devolvemos en /register) ---
class UserResponse(BaseModel):
    id: int
    email: EmailStr

    # Configuración para que Pydantic funcione bien con SQLAlchemy
    class Config:
        orm_mode = True 

# --- Modelo para la respuesta del token (lo que devolvemos en /login) ---
class Token(BaseModel):
    access_token: str
    token_type: str

# --- Modelo para el contenido decodificado del token (lo que devolvemos en /verify) ---
class TokenPayload(BaseModel):
    sub: EmailStr | None = None # Subject (el email del usuario)
    exp: int | None = None      # Expiration time