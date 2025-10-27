from sqlalchemy import Column, Integer, String, Float, ForeignKey
from .db import Base

class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    
    # Esta será la referencia al ID del usuario en la tabla `users`
    # Es crucial que sea única para que un usuario no tenga dos cuentas
    user_id = Column(Integer, unique=True, index=True, nullable=False)
    
    # El saldo de la cuenta. 
    # Usamos Float por simplicidad, en producción se usaría Decimal.
    balance = Column(Float, nullable=False, default=0.0)
    
    currency = Column(String(10), nullable=False, default="USD")