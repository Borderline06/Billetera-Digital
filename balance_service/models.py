"""Define los modelos de las tablas 'accounts' y 'group_accounts' usando SQLAlchemy ORM."""

from sqlalchemy import Column, Integer, String, Float
# Importación absoluta desde el módulo db.py del mismo directorio
from db import Base

class Account(Base):
    """
    Modelo SQLAlchemy que representa la tabla 'accounts'.
    Almacena el saldo de las billeteras digitales individuales (BDI).
    """
    __tablename__ = "accounts"

    # Clave primaria autoincremental
    id = Column(Integer, primary_key=True, index=True)

    # Clave foránea (lógica) al ID del usuario en el servicio de autenticación.
    # Se asegura que cada usuario tenga solo una cuenta individual.
    user_id = Column(Integer, unique=True, index=True, nullable=False)

    # Saldo actual de la cuenta individual.
    # NOTA: Float se usa por simplicidad; en producción se recomienda usar Decimal para precisión monetaria.
    balance = Column(Float, nullable=False, default=0.0)

    # Moneda de la cuenta (ej. "PEN", "USD").
    currency = Column(String(10), nullable=False, default="USD")


class GroupAccount(Base):
    """
    Modelo SQLAlchemy que representa la tabla 'group_accounts'.
    Almacena el saldo de las billeteras digitales grupales (BDG).
    """
    __tablename__ = "group_accounts"

    # Clave primaria autoincremental
    id = Column(Integer, primary_key=True, index=True)

    # Clave foránea (lógica) al ID del grupo en el servicio de grupos.
    # Se asegura que cada grupo tenga solo una cuenta de saldo.
    group_id = Column(Integer, unique=True, index=True, nullable=False)

    # Saldo actual de la cuenta grupal.
    balance = Column(Float, nullable=False, default=0.0)

    # Moneda de la cuenta grupal.
    currency = Column(String(10), nullable=False, default="USD")