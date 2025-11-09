"""Módulo para la conexión y configuración del schema en la base de datos Cassandra."""

import os
import logging
import time
from typing import Optional 

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

REPLICATION_FACTOR = int(os.getenv("CASSANDRA_REPLICATION_FACTOR", 1))

def get_cassandra_session() -> Optional[Session]:
    """
    Establece conexión con el clúster de Cassandra y devuelve un objeto Session.
    Implementa una política de reintentos robusta.
    """
    attempts = 0
    max_attempts = 30 # 30 intentos
    wait_time = 10    # 10 segundos (Total: 5 minutos de espera)

    cluster: Optional[Cluster] = None 

    while attempts < max_attempts:
        try:
            # 1. Crear un NUEVO objeto Cluster en CADA intento
            cluster = Cluster(
                [CASSANDRA_HOST],
                load_balancing_policy=DCAwareRoundRobinPolicy(local_dc='datacenter1'),
                port=9042,
                # Añadimos un timeout de conexión más corto para fallar rápido
                connect_timeout=5 
            )

            # 2. Intentar conectar
            session = cluster.connect()

            logger.info("Conexión a Cassandra establecida exitosamente.")
            return session 

        except Exception as e:
            attempts += 1
            logger.warning(f"Esperando a Cassandra... Intento {attempts}/{max_attempts}. Error: {e}")

            
            # Cerramos el cluster SÓLO SI llegó a crearse antes de fallar
            if cluster:
                 cluster.shutdown()

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
                id uuid PRIMARY KEY,           
                user_id int,                   
                source_wallet_type text,       
                source_wallet_id text,         
                destination_wallet_type text, 
                destination_wallet_id text,   
                type text,                    
                amount double,                 
                currency text,                
                status text,                  
                created_at timestamp,         
                updated_at timestamp,         
                metadata text                  
            );
        """)

        # --- 3. Crear Tabla 'idempotency_keys' ---
        logger.info("Verificando/Creando tabla 'idempotency_keys'...")
        session.execute("""
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key uuid PRIMARY KEY,          
                transaction_id uuid           
            );
        """)

       
        # Esta es la tabla optimizada para LEER el historial de un usuario.
        # Duplicamos datos (normal en NoSQL) para tener consultas rápidas.
        logger.info("Verificando/Creando tabla 'transactions_by_user'...")
        session.execute(f"""
        CREATE TABLE IF NOT EXISTS {KEYSPACE}.transactions_by_user (
            user_id int,
            created_at timestamp,
            id uuid,
            source_wallet_type text,
            source_wallet_id text,
            destination_wallet_type text,
            destination_wallet_id text,
            type text,
            amount decimal,
            currency text,
            status text,
            metadata text,
            updated_at timestamp,
            PRIMARY KEY (user_id, created_at, id)
        ) WITH CLUSTERING ORDER BY (created_at DESC);
        """)
        

        # --- 4. Crear Índice Secundario en 'user_id' ---
        logger.info("Verificando/Creando índice en 'transactions(user_id)'...")
        session.execute("""
            CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions (user_id);
        """)
        

        logger.info("Schema de Cassandra verificado/creado exitosamente.")

    except Exception as e:
        logger.error(f"Error fatal al crear/verificar el schema de Cassandra: {e}", exc_info=True)
        # Es crucial que el schema exista; si falla aquí, el servicio no puede operar.
        raise e # Relanzamos la excepción para detener potencialmente el inicio del servicio.