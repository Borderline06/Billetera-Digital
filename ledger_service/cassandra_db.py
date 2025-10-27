# ledger_service/cassandra_db.py
import os
from cassandra.cluster import Cluster, Session
from cassandra.policies import DCAwareRoundRobinPolicy
from dotenv import load_dotenv
import logging
import time

load_dotenv()

CASSANDRA_HOST = os.getenv("CASSANDRA_HOST", "cassandra1")
KEYSPACE = "wallet_ledger"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_cassandra_session() -> Session | None:
    """
    Se conecta a Cassandra y devuelve un objeto de sesión.
    Reintenta la conexión varias veces si el cluster aún está arrancando.
    """
    cluster = Cluster(
        [CASSANDRA_HOST],
        load_balancing_policy=DCAwareRoundRobinPolicy(local_dc='datacenter1'), # Política de balanceo
        port=9042
    )
    
    attempts = 0
    max_attempts = 10
    wait_time = 5 # segundos
    
    while attempts < max_attempts:
        try:
            session = cluster.connect()
            logger.info("✅ Conectado a Cassandra.")
            return session
        except Exception as e:
            logger.warning(f"Esperando a Cassandra... (Intento {attempts+1}/{max_attempts}). Error: {e}")
            attempts += 1
            time.sleep(wait_time)
            
    logger.error("❌ No se pudo conectar a Cassandra después de varios intentos.")
    return None

def create_keyspace_and_tables(session: Session):
    """
    Crea el Keyspace (BD) y las tablas si no existen.
    Esta es la estructura CORRECTA que incluye estado e idempotencia.
    """
    try:
        # --- 1. Crear el Keyspace (Base de Datos) ---
        # SimpleStrategy es para un solo datacenter (nuestro cluster de Docker)
        # replication_factor = 3 significa que los datos se copiarán en los 3 nodos
        session.execute(f"""
            CREATE KEYSPACE IF NOT EXISTS {KEYSPACE}
            WITH replication = {{
                'class': 'SimpleStrategy',
                'replication_factor': 3
            }};
        """)
        
        # Le decimos a la sesión que use nuestro keyspace
        session.set_keyspace(KEYSPACE)

        # --- 2. Crear la Tabla de Transacciones (El Ledger) ---
        session.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id uuid PRIMARY KEY,
                user_id int,
                type text,          # 'DEPOSIT' o 'TRANSFER'
                amount double,
                status text,        # 'PENDING', 'COMPLETED', 'FAILED'
                created_at timestamp,
                updated_at timestamp,
                metadata text       # ej. '{"to_bank": "BankB", "to_account": "..."}'
            );
        """)

        # --- 3. Crear la Tabla de Idempotencia (Seguridad) ---
        # Esto previene que un cliente envíe el mismo pago dos veces
        session.execute("""
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key uuid PRIMARY KEY,
                transaction_id uuid
            );
        """)
        
        # --- 4. (Opcional) Índices para búsquedas ---
        # Creamos un índice secundario para poder buscar transacciones por usuario
        session.execute("""
            CREATE INDEX IF NOT EXISTS ON transactions (user_id);
        """)
        
        logger.info("✅ Keyspace y tablas de Cassandra verificados/creados.")
    
    except Exception as e:
        logger.error(f"❌ Error al crear schema de Cassandra: {e}")
        raise e