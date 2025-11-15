"""Define los modelos de las tablas 'accounts' y 'group_accounts' usando SQLAlchemy ORM."""
from sqlalchemy import Column, Integer, String, Float, UniqueConstraint, ForeignKey, Numeric, DateTime, func, Enum as SQLEnum
from decimal import Decimal
import enum
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
    balance = Column(Numeric(10, 2), nullable=False, default=Decimal('0.00'))

    # Moneda de la cuenta 
    currency = Column(String(10), nullable=False, default="USD")


class GroupAccount(Base):
    """Modelo de la cuenta de una Billetera Grupal (BDG)."""
    __tablename__ = "group_accounts"

    
    group_id = Column(Integer, primary_key=True, index=True) 

    balance = Column(Numeric(10, 2), nullable=False, default=0.00)

    
    version = Column(Integer, nullable=False, default=1) 

    __mapper_args__ = {
        "version_id_col": version
    }

# ... (al final de models.py)

class LoanStatus(str, enum.Enum):
    """Define el estado de un préstamo."""
    ACTIVE = "active"
    PAID = "paid"

class Loan(Base):
    """
    Modelo SQLAlchemy para la tabla 'loans'.
    Rastrea los préstamos (deudas) de cada usuario.
    """
    __tablename__ = "loans"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("accounts.user_id"), nullable=False, unique=True) # ¡Un préstamo activo por usuario!

    principal_amount = Column(Numeric(10, 2), nullable=False) # El monto original
    outstanding_balance = Column(Numeric(10, 2), nullable=False) # Cuánto debe (con interés)
    interest_rate = Column(Numeric(5, 2), nullable=False, default=Decimal('5.00')) # Ej. 5%

    status = Column(SQLEnum(LoanStatus), nullable=False, default=LoanStatus.ACTIVE)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    due_date = Column(DateTime(timezone=True), nullable=True) # (Para V2: fecha de pago)