# group_service/schemas.py (Limpio)

from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime
from typing import Optional, List
from models import GroupRole

class GroupCreate(BaseModel):
    """Schema para crear un grupo. El líder viene por Header."""
    name: str = Field(..., min_length=3, max_length=100)
    

class GroupInviteRequest(BaseModel):
    """Schema para invitar. El invitador viene por Header."""
    user_id_to_invite: int = Field(..., description="ID del usuario a invitar")
   

class GroupMemberResponse(BaseModel):
    """Schema para mostrar un miembro."""
    user_id: int
    role: GroupRole
    group_id: int 

    model_config = ConfigDict(from_attributes=True)

class GroupResponse(BaseModel):
    """Schema para mostrar un grupo completo."""
    id: int
    name: str
    leader_user_id: int
    members: List[GroupMemberResponse] = []

    model_config = ConfigDict(from_attributes=True)