"""Define el modelo de la tabla 'users' usando SQLAlchemy ORM."""

from sqlalchemy import Column, Integer, String, Boolean, DateTime

from db import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)  # 👈 nuevo campo
    email = Column(String(255), unique=True, index=True, nullable=False)
    phone_number = Column(String(20), unique=True, index=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)

    # --- CAMBIOS ---
    # 1. El usuario ahora se crea como inactivo por defecto
    is_active = Column(Boolean, default=False)
    
    # 2. Nuevos campos para el proceso de verificación
    verification_code = Column(String(6), nullable=True)
    code_expires_at = Column(DateTime, nullable=True)