"""Define el modelo de la tabla 'users' usando SQLAlchemy ORM."""

from sqlalchemy import Column, Integer, String
# Importación absoluta desde el módulo db.py del mismo directorio
from db import Base

class User(Base):
    """
    Modelo SQLAlchemy que representa la tabla 'users' en la base de datos.
    Almacena la información de autenticación de los usuarios.
    """
    __tablename__ = "users"

    # Clave primaria autoincremental
    id = Column(Integer, primary_key=True, index=True)

    # Email del usuario, usado como identificador único para el login
    email = Column(String(255), unique=True, index=True, nullable=False)

    # Hash de la contraseña del usuario (generado con bcrypt)
    hashed_password = Column(String(255), nullable=False)

    # Nota: No se almacena la contraseña en texto plano por seguridad.
    # No se incluye columna 'balance' aquí; se gestiona en 'balance_service'.