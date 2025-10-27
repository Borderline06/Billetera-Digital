# balance_service/schemas.py
from pydantic import BaseModel, Field

# --- Esquema para crear una cuenta ---
# (Usado por auth_service al registrar un usuario)
class AccountCreate(BaseModel):
    user_id: int

# --- Esquema para actualizar el saldo ---
# (Usado por ledger_service para depósitos y transferencias)
class BalanceUpdate(BaseModel):
    user_id: int
    amount: float = Field(
        ..., 
        gt=0, 
        description="El monto debe ser positivo"
    )

# --- Esquema para verificar el saldo (GET) ---
# (Usado por ledger_service antes de una transferencia)
class BalanceCheck(BaseModel):
    user_id: int
    amount: float = Field(
        ..., 
        gt=0, 
        description="El monto a verificar debe ser positivo"
    )

# --- Esquema de respuesta (lo que devolvemos) ---
class Account(BaseModel):
    id: int
    user_id: int
    balance: float
    currency: str

    # Configuración para que Pydantic funcione con SQLAlchemy
    class Config:
        orm_mode = True