"""Modelos Pydantic (schemas) para validación de datos en el Balance Service."""

from pydantic import BaseModel, Field, ConfigDict

# --- Schemas para Cuentas Individuales (BDI) ---

class AccountCreate(BaseModel):
    """Schema para la solicitud de creación de una cuenta individual."""
    user_id: int

class BalanceUpdate(BaseModel):
    """Schema para solicitar una actualización (crédito/débito) del saldo individual."""
    user_id: int
    amount: float = Field(..., gt=0, description="El monto para actualizar debe ser positivo.")

class BalanceCheck(BaseModel):
    """Schema para solicitar la verificación de fondos suficientes en una cuenta individual."""
    user_id: int
    amount: float = Field(..., gt=0, description="El monto a verificar debe ser positivo.")

class AccountResponse(BaseModel):
    """Schema para la respuesta al obtener detalles de una cuenta individual."""
    id: int
    user_id: int
    balance: float
    currency: str

    # Configuración Pydantic v2+ para mapeo desde modelos ORM
    model_config = ConfigDict(from_attributes=True)


# --- Schemas para Cuentas Grupales (BDG) ---

class GroupAccountCreate(BaseModel):
    """Schema para la solicitud de creación de una cuenta grupal."""
    group_id: int

class GroupBalanceUpdate(BaseModel):
    """Schema para solicitar una actualización (crédito/débito) del saldo grupal."""
    group_id: int
    amount: float = Field(..., gt=0, description="El monto para actualizar debe ser positivo.")

class GroupAccountResponse(BaseModel):
    """Schema para la respuesta al obtener detalles de una cuenta grupal."""
    id: int
    group_id: int
    balance: float
    currency: str

    # Configuración Pydantic v2+ para mapeo desde modelos ORM
    model_config = ConfigDict(from_attributes=True)