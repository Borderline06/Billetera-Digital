# group_service/schemas.py (Versión Corregida y Limpia)

"""Modelos Pydantic (schemas) para validación de datos en el Group Service."""

from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime
from typing import Optional, List
from models import GroupRole, GroupMemberStatus # ¡La importación clave!

# --- Schemas de Entrada (Input) ---

class GroupCreate(BaseModel):
    """Schema para crear un grupo. El líder viene por Header."""
    name: str = Field(..., min_length=3, max_length=100)
    # El user_id (líder) vendrá por Header (X-User-ID), NO aquí.

class GroupInviteRequest(BaseModel):
    """Schema para invitar. El invitador viene por Header."""
    user_id_to_invite: int = Field(..., description="ID del usuario a invitar")
    # El user_id (invitador) vendrá por Header (X-User-ID), NO aquí.


# --- Schemas de Salida (Respuesta) ---

class GroupMemberResponse(BaseModel):
    """
    Schema para representar la información de un miembro dentro de un grupo.
    Usado para las listas anidadas (ej. group.members).
    ¡ESTA ES LA ÚNICA DEFINICIÓN!
    """
    user_id: int
    role: GroupRole # Muestra el rol ('leader' o 'member')
    group_id: int  # <-- El campo que faltaba en la definición duplicada
    status: GroupMemberStatus

    # Configuración Pydantic v2+ para mapeo desde modelos ORM
    model_config = ConfigDict(from_attributes=True)

class GroupResponse(BaseModel):
    """Schema para mostrar un grupo completo."""
    id: int
    name: str
    leader_user_id: int
    created_at: datetime # <-- ¡AÑADE ESTA LÍNEA!
    members: List[GroupMemberResponse] = []

    model_config = ConfigDict(from_attributes=True)