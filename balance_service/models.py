"""Define los modelos de las tablas 'accounts' y 'group_accounts' usando SQLAlchemy ORM."""

from sqlalchemy import Column, Integer, String, Float, UniqueConstraint, func, Numeric
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
    """Modelo de la cuenta de una Billetera Grupal (BDG)."""
    __tablename__ = "group_accounts"

    # Usamos el group_id del group_service como PK
    group_id = Column(Integer, primary_key=True, index=True) 

    balance = Column(Numeric(10, 2), nullable=False, default=0.00)

    # Versión para control de concurrencia (optimistic locking)
    version = Column(Integer, nullable=False, default=1) 

    __mapper_args__ = {
        "version_id_col": version
    }