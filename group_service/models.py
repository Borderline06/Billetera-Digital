"""Define los modelos de las tablas 'groups' y 'group_members' usando SQLAlchemy ORM."""

import enum
from sqlalchemy import Column, Integer, String, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import relationship
# Importación absoluta desde el módulo db.py del mismo directorio
from db import Base

class GroupRole(str, enum.Enum):
    """Define los roles posibles para un miembro dentro de un grupo."""
    LEADER = "leader" # Rol de líder/administrador del grupo
    MEMBER = "member" # Rol de miembro estándar

class Group(Base):
    """
    Modelo SQLAlchemy que representa la tabla 'groups'.
    Almacena la información básica de una Billetera Digital Grupal (BDG).
    """
    __tablename__ = "groups"

    # Clave primaria autoincremental del grupo
    id = Column(Integer, primary_key=True, index=True)
    # Nombre del grupo (ej. "Viaje Amigos")
    name = Column(String(100), nullable=False, index=True)
    # ID del usuario (del auth_service) que creó y lidera el grupo
    leader_user_id = Column(Integer, nullable=False, index=True)

    # Relación uno-a-muchos con GroupMember.
    # Permite acceder a group.members para obtener la lista de miembros.
    # cascade="all, delete-orphan" podría añadirse si quisiéramos borrar miembros al borrar el grupo.
    members = relationship("GroupMember", back_populates="group")

class GroupMember(Base):
    """
    Modelo SQLAlchemy que representa la tabla 'group_members'.
    Establece la relación entre un usuario y un grupo al que pertenece,
    incluyendo su rol dentro del grupo.
    """
    __tablename__ = "group_members"

    # Clave primaria autoincremental de la membresía
    id = Column(Integer, primary_key=True, index=True)
    # Clave foránea que enlaza con la tabla 'groups'
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    # ID del usuario (del auth_service) que es miembro
    user_id = Column(Integer, nullable=False, index=True)
    # Rol del usuario dentro de este grupo específico (líder o miembro)
    role = Column(SQLEnum(GroupRole), nullable=False, default=GroupRole.MEMBER)

    # Relación muchos-a-uno con Group.
    # Permite acceder a member.group para obtener el grupo al que pertenece.
    group = relationship("Group", back_populates="members")

    # Podríamos añadir una restricción UNIQUE(group_id, user_id) a nivel de BD
    # para asegurar que un usuario solo pueda ser miembro una vez por grupo.