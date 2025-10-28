"""Modelos Pydantic (schemas) para validación de datos en el Group Service."""

from pydantic import BaseModel, Field, ConfigDict
from typing import List
# Importación absoluta desde el módulo models.py del mismo directorio
from models import GroupRole

# --- Schemas de Entrada (Input) ---

class GroupCreate(BaseModel):
    """Schema para la solicitud de creación de un nuevo grupo (BDG)."""
    name: str = Field(..., min_length=3, max_length=100, description="Nombre del grupo.")
    # El ID del líder se obtiene implícitamente del token JWT a través del Gateway.

class GroupInvite(BaseModel):
    """Schema para la solicitud de invitación de un nuevo miembro a un grupo."""
    user_id_to_invite: int = Field(..., description="ID del usuario a invitar.")

# --- Schemas de Salida (Respuesta) ---

class GroupMemberResponse(BaseModel):
    """Schema para representar la información de un miembro dentro de un grupo."""
    user_id: int
    role: GroupRole # Muestra el rol ('leader' o 'member')

    # Configuración Pydantic v2+ para mapeo desde modelos ORM
    model_config = ConfigDict(from_attributes=True)

class GroupResponse(BaseModel):
    """Schema para la respuesta al obtener detalles de un grupo."""
    id: int
    name: str
    leader_user_id: int
    # Incluye una lista de los miembros actuales y sus roles
    members: List[GroupMemberResponse] = []

    # Configuración Pydantic v2+ para mapeo desde modelos ORM
    model_config = ConfigDict(from_attributes=True)