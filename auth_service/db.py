import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from dotenv import load_dotenv

# Carga las variables de entorno desde el archivo .env
load_dotenv()

# Lee las credenciales de la base de datos
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")

SQLALCHEMY_DATABASE_URL = f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}"

# Crea el "motor" de SQLAlchemy
engine = create_engine(SQLALCHEMY_DATABASE_URL)

# Crea una fábrica de sesiones (así es como hablaremos con la BD)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Esta es la clase base de la que heredarán nuestros modelos (tablas)
Base = declarative_base()

# Función de utilidad para obtener una sesión de BD en nuestras rutas
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()