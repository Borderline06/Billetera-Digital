"""Configuración de la conexión a la base de datos MariaDB usando SQLAlchemy para el Group Service."""

import os
import logging
from sqlalchemy import create_engine, exc
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from dotenv import load_dotenv

# Configuración del logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Carga variables de entorno desde el archivo .env
load_dotenv()

# Lee las credenciales de la base de datos desde el entorno
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")

# Valida que las variables necesarias estén presentes
required_db_vars = {"DB_USER", "DB_PASS", "DB_HOST", "DB_NAME"}
missing_vars = required_db_vars - set(os.environ)
if missing_vars:
    logger.error(f"Faltan variables de entorno para la base de datos: {', '.join(missing_vars)}")
    # Considerar lanzar excepción o salir

SQLALCHEMY_DATABASE_URL = f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}"

# Crea el motor (Engine) de SQLAlchemy
engine = None
try:
    engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True)
    # Intenta conectar para verificar credenciales al inicio
    with engine.connect() as connection:
        logger.info("Conexión a la base de datos establecida exitosamente.")
except exc.SQLAlchemyError as e:
    logger.error(f"Error al conectar con la base de datos: {e}", exc_info=True)
    # El servicio no podrá funcionar sin la base de datos.
    engine = None

# Crea una fábrica de sesiones (SessionLocal)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine) if engine else None

# Crea una clase base (Base) para los modelos declarativos
Base = declarative_base()

# --- Función de Dependencia para FastAPI ---
def get_db():
    """
    Generador de dependencia de FastAPI para obtener una sesión de base de datos.
    Asegura que la sesión se cierre correctamente después de cada petición.
    """
    if SessionLocal is None:
        logger.error("La fábrica de sesiones de base de datos no está inicializada.")
        raise HTTPException(status_code=503, detail="Servicio de base de datos no disponible.")

    db = SessionLocal()
    try:
        yield db # Proporciona la sesión a la ruta
    except exc.SQLAlchemyError as e:
        logger.error(f"Error de base de datos durante la petición: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Error interno de base de datos.")
    except Exception as e:
         logger.error(f"Error inesperado durante la petición: {e}", exc_info=True)
         db.rollback()
         raise HTTPException(status_code=500, detail="Error interno del servidor.")
    finally:
        db.close() # Cierra la sesión al finalizar