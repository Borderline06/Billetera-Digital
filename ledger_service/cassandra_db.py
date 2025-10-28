"""Módulo para la conexión y configuración del schema en la base de datos Cassandra."""

import os
import logging
import time
from typing import Optional # Importado para type hint más preciso

from cassandra.cluster import Cluster, Session
from cassandra.policies import DCAwareRoundRobinPolicy
from dotenv import load_dotenv

# Carga variables de entorno
load_dotenv()

# Configuración del logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Lee la configuración de Cassandra desde el entorno
CASSANDRA_HOST = os.getenv("CASSANDRA_HOST", "cassandra1") # Nodo(s) semilla
KEYSPACE = "wallet_ledger" # Nombre de nuestro espacio de claves (base de datos)
# Para desarrollo local, RF=1 es más simple aunque tengamos 3 nodos. En producción usar RF=3.
REPLICATION_FACTOR = int(os.getenv("CASSANDRA_REPLICATION_FACTOR", 1))

def get_cassandra_session() -> Optional[Session]:
    """
    Establece conexión con el clúster de Cassandra y devuelve un objeto Session.
    Implementa una política de reintentos para esperar a que el clúster esté disponible.

    Returns:
        Un objeto Session si la conexión es exitosa, None en caso contrario.
    """
    # Usamos DCAwareRoundRobinPolicy asumiendo un solo datacenter llamado 'datacenter1'
    # (es el nombre por defecto en las instalaciones de Cassandra en Docker)
    cluster = Cluster(
        [CASSANDRA_HOST],
        load_balancing_policy=DCAwareRoundRobinPolicy(local_dc='datacenter1'),
        port=9042
    )

    attempts = 0
    max_attempts = 20
    wait_time = 10 # segundos

    while attempts < max_attempts:
        try:
            session = cluster.connect()
            logger.info("Conexión a Cassandra establecida exitosamente.")
            return session
        except Exception as e:
            attempts += 1
            logger.warning(f"Esperando a Cassandra... Intento {attempts}/{max_attempts}. Error: {e}")
            if attempts < max_attempts:
                time.sleep(wait_time)

    logger.error("No se pudo conectar a Cassandra después de %d intentos.", max_attempts)
    return None

def create_keyspace_and_tables(session: Session):
    """
    Crea el keyspace y las tablas necesarias en Cassandra si no existen.
    Esta función debe ejecutarse al inicio del servicio.

    Args:
        session: La sesión activa de Cassandra.
    """
    try:
        # --- 1. Crear Keyspace ---
        # SimpleStrategy es adecuado para un solo datacenter.
        logger.info(f"Verificando/Creando Keyspace '{KEYSPACE}' con RF={REPLICATION_FACTOR}...")
        session.execute(f"""
            CREATE KEYSPACE IF NOT EXISTS {KEYSPACE}
            WITH replication = {{
                'class': 'SimpleStrategy',
                'replication_factor': {REPLICATION_FACTOR}
            }};
        """)
        session.set_keyspace(KEYSPACE) # Cambiamos al keyspace correcto para las siguientes operaciones
        logger.info(f"Usando Keyspace '{KEYSPACE}'.")

        # --- 2. Crear Tabla 'transactions' (Ledger) ---
        logger.info("Verificando/Creando tabla 'transactions'...")
        session.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id uuid PRIMARY KEY,           # Identificador único de la transacción
                user_id int,                   # ID del usuario principal asociado (ej. quien inicia)
                source_wallet_type text,       # Tipo de billetera origen ('BDI', 'BDG', 'EXTERNAL')
                source_wallet_id text,         # ID de la billetera origen (user_id, group_id, u otro identificador)
                destination_wallet_type text,  # Tipo de billetera destino ('BDI', 'BDG', 'EXTERNAL_BANK')
                destination_wallet_id text,    # ID de la billetera destino (user_id, group_id, nro_celular, etc.)
                type text,                     # Tipo de operación ('DEPOSIT', 'TRANSFER', 'CONTRIBUTION', 'WITHDRAWAL')
                amount double,                 # Monto de la transacción
                currency text,                 # Moneda (ej. 'PEN', 'USD') - Añadido para claridad
                status text,                   # Estado ('PENDING', 'COMPLETED', 'FAILED_FUNDS', 'FAILED_REMOTE', etc.)
                created_at timestamp,          # Marca de tiempo de creación
                updated_at timestamp,          # Marca de tiempo de última actualización
                metadata text                  # JSON como texto con detalles adicionales (ej. ID de transacción externa)
            );
        """)

        # --- 3. Crear Tabla 'idempotency_keys' ---
        logger.info("Verificando/Creando tabla 'idempotency_keys'...")
        session.execute("""
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key uuid PRIMARY KEY,          # La clave de idempotencia proporcionada por el cliente
                transaction_id uuid            # El ID de la transacción asociada a esa clave
            );
        """)

        # --- 4. Crear Índice Secundario en 'user_id' ---
        logger.info("Verificando/Creando índice en 'transactions(user_id)'...")
        session.execute("""
            CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions (user_id);
        """)
        # Considerar índices adicionales según las consultas frecuentes (ej. source_wallet_id, destination_wallet_id)

        logger.info("Schema de Cassandra verificado/creado exitosamente.")

    except Exception as e:
        logger.error(f"Error fatal al crear/verificar el schema de Cassandra: {e}", exc_info=True)
        # Es crucial que el schema exista; si falla aquí, el servicio no puede operar.
        raise e # Relanzamos la excepción para detener potencialmente el inicio del servicio.