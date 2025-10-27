# ledger_service/schemas.py
from pydantic import BaseModel, Field, UUID4
from datetime import datetime

# --- Esquemas de Entrada (Input) ---

class DepositRequest(BaseModel):
    user_id: int
    amount: float = Field(..., gt=0, description="El monto debe ser positivo")

class TransferRequest(BaseModel):
    user_id: int
    amount: float = Field(..., gt=0, description="El monto debe ser positivo")
    to_bank: str
    to_account: str

# --- Esquemas de Salida (Respuesta) ---

class Transaction(BaseModel):
    id: UUID4
    user_id: int
    type: str
    amount: float
    status: str
    created_at: datetime
    updated_at: datetime
    metadata: str | None = None

    class Config:
        orm_mode = True # Para compatibilidad con objetos de Cassandra