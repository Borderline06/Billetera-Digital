from sqlalchemy import Column, Integer, String
from .db import Base # Importamos la Base que creamos en db.py

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    
    # Usamos email como el "username" único
    email = Column(String(255), unique=True, index=True, nullable=False)
    
    # Aquí guardaremos la contraseña ya encriptada (hasheada)
    hashed_password = Column(String(255), nullable=False)