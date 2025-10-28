"""Modelos Pydantic (schemas) para validación de datos en el Ledger Service."""

from pydantic import BaseModel, Field, UUID4, ConfigDict
from datetime import datetime
from typing import Optional # Añadido para Optional

# --- Esquemas de Entrada (Input) ---

class DepositRequest(BaseModel):
    """Schema para la solicitud de depósito en una BDI."""
    # Nota: user_id es inyectado por el Gateway desde el token.
    # El cliente NO necesita enviarlo, pero lo definimos aquí
    # para la validación interna después de la inyección.
    user_id: int
    amount: float = Field(..., gt=0, description="El monto a depositar debe ser positivo.")

class TransferRequest(BaseModel):
    """Schema para la solicitud de transferencia BDI -> BDI (externa)."""
    user_id: int # Inyectado por el Gateway
    amount: float = Field(..., gt=0, description="El monto a transferir debe ser positivo.")
    to_bank: str # Ej. "HAPPY_MONEY"
    # Identificador del destinatario en el otro banco (número de celular)
    destination_phone_number: str = Field(..., min_length=9, max_length=15, description="Número de celular del destinatario.")

class ContributionRequest(BaseModel):
    """Schema para la solicitud de aporte BDI -> BDG."""
    user_id: int # Inyectado por el Gateway (quien aporta)
    group_id: int # ID del grupo que recibe el aporte
    amount: float = Field(..., gt=0, description="El monto a aportar debe ser positivo.")

# --- Esquema de Salida (Respuesta) ---

class Transaction(BaseModel):
    """Schema para representar una transacción registrada en el ledger."""
    id: UUID4
    user_id: int
    source_wallet_type: Optional[str] = None
    source_wallet_id: Optional[str] = None # Puede ser int o str según el tipo
    destination_wallet_type: Optional[str] = None
    destination_wallet_id: Optional[str] = None # Puede ser int o str
    type: str
    amount: float
    currency: Optional[str] = None # Añadido para coincidir con la tabla
    status: str
    created_at: datetime
    updated_at: datetime
    metadata: Optional[str] = None # JSON como string

    # Configuración Pydantic v2+ para mapeo desde modelos ORM/Cassandra
    model_config = ConfigDict(from_attributes=True)