# ledger_service/main.py
import os
import httpx
import uuid
import json
import logging
from datetime import datetime, timezone
from fastapi import FastAPI, Depends, HTTPException, status, Header
from cassandra.cluster import Session
from cassandra.query import SimpleStatement

# Importaciones locales
from . import cassandra_db
from . import schemas
from .utils import load_env_vars # (Crearemos este archivo)

# Configuración
load_env_vars()
BALANCE_SERVICE_URL = os.getenv("BALANCE_SERVICE_URL")
MOCK_BANKB_URL = os.getenv("MOCK_BANKB_URL")
KEYSPACE = cassandra_db.KEYSPACE

# Configura un logger básico
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Inicialización de la App y Base de Datos ---
app = FastAPI(title="Ledger Service - Bank A")
db_session: Session | None = None

@app.on_event("startup")
def startup_event():
    """Se ejecuta cuando FastAPI arranca."""
    global db_session
    db_session = cassandra_db.get_cassandra_session()
    if db_session:
        cassandra_db.create_keyspace_and_tables(db_session)
    else:
        logger.critical("FATAL: No se pudo conectar a Cassandra. El servicio no funcionará.")
        # En un sistema real, esto debería detener el servicio.

@app.on_event("shutdown")
def shutdown_event():
    """Se ejecuta cuando FastAPI se apaga."""
    if db_session:
        db_session.cluster.shutdown()
        logger.info("Conexión a Cassandra cerrada.")

def get_db():
    """Función de dependencia para obtener la sesión de Cassandra."""
    if db_session is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Servicio de base de datos no disponible")
    return db_session

# --- Función de Seguridad: Idempotencia ---
def check_idempotency(session: Session, key: str) -> uuid.UUID | None:
    """
    Verifica si una clave de idempotencia ya fue usada.
    Devuelve el ID de la transacción si ya existe, o None si es nueva.
    """
    try:
        key_uuid = uuid.UUID(key) # Validamos que sea un UUID
        query = SimpleStatement(f"SELECT transaction_id FROM {KEYSPACE}.idempotency_keys WHERE key = %s")
        result = session.execute(query, (key_uuid,)).one()
        if result:
            return result.transaction_id
        return None
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Idempotency-Key inválida (debe ser UUID)")


# --- Endpoints de la API ---

@app.post("/deposit", response_model=schemas.Transaction, status_code=status.HTTP_201_CREATED)
async def deposit(
    req: schemas.DepositRequest, 
    idempotency_key: str | None = Header(None), 
    db: Session = Depends(get_db)
):
    """
    Procesa un depósito.
    1. Verifica idempotencia.
    2. Crea el registro en Cassandra como PENDING.
    3. Llama al balance_service para ACREDITAR.
    4. Actualiza el registro a COMPLETED.
    """
    if idempotency_key is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Idempotency-Key es requerida")

    # 1. Verificar Idempotencia
    existing_tx_id = check_idempotency(db, idempotency_key)
    if existing_tx_id:
        # La petición es un duplicado, devolvemos la transacción original
        tx = db.execute(f"SELECT * FROM {KEYSPACE}.transactions WHERE id = %s", (existing_tx_id,)).one()
        return tx

    tx_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    metadata_json = json.dumps({"source": "deposit"})

    # 2. Registrar Intento (PENDING)
    db.execute(
        f"""
        INSERT INTO {KEYSPACE}.transactions (id, user_id, type, amount, status, created_at, updated_at, metadata)
        VALUES (%s, %s, 'DEPOSIT', %s, 'PENDING', %s, %s, %s)
        """,
        (tx_id, req.user_id, req.amount, now, now, metadata_json)
    )

    # 3. Llamar al Balance Service
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{BALANCE_SERVICE_URL}/balance/credit",
                json={"user_id": req.user_id, "amount": req.amount}
            )
            response.raise_for_status() # Lanza error si balance_service falla

        except Exception as e:
            # Si balance_service falla, marcamos la tx como FAILED
            db.execute(f"UPDATE {KEYSPACE}.transactions SET status = 'FAILED', updated_at = %s WHERE id = %s", (datetime.now(timezone.utc), tx_id))
            logger.error(f"Fallo al acreditar balance para tx {tx_id}: {e}")
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Balance Service falló: {e}")

    # 4. Registrar Idempotencia y Marcar como COMPLETED
    db.execute(f"INSERT INTO {KEYSPACE}.idempotency_keys (key, transaction_id) VALUES (%s, %s)", (uuid.UUID(idempotency_key), tx_id))
    db.execute(f"UPDATE {KEYSPACE}.transactions SET status = 'COMPLETED', updated_at = %s WHERE id = %s", (datetime.now(timezone.utc), tx_id))

    logger.info(f"Depósito COMPLETED para user_id {req.user_id}, tx_id {tx_id}")
    tx = db.execute(f"SELECT * FROM {KEYSPACE}.transactions WHERE id = %s", (tx_id,)).one()
    return tx


@app.post("/transfer", response_model=schemas.Transaction, status_code=status.HTTP_201_CREATED)
async def transfer(
    req: schemas.TransferRequest, 
    idempotency_key: str | None = Header(None), 
    db: Session = Depends(get_db)
):
    """
    Procesa una transferencia externa (Banco A -> Banco B).
    Sigue el flujo del BPMN.
    """
    if idempotency_key is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Idempotency-Key es requerida")

    # 1. Verificar Idempotencia
    existing_tx_id = check_idempotency(db, idempotency_key)
    if existing_tx_id:
        return db.execute(f"SELECT * FROM {KEYSPACE}.transactions WHERE id = %s", (existing_tx_id,)).one()

    tx_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    metadata = {"to_bank": req.to_bank, "to_account": req.to_account}
    
    # 2. Registrar Intento (PENDING)
    db.execute(
        f"""
        INSERT INTO {KEYSPACE}.transactions (id, user_id, type, amount, status, created_at, updated_at, metadata)
        VALUES (%s, %s, 'TRANSFER', %s, 'PENDING', %s, %s, %s)
        """,
        (tx_id, req.user_id, req.amount, now, now, json.dumps(metadata))
    )

    async with httpx.AsyncClient() as client:
        try:
            # 3. Verificar Fondos (Paso 7 del BPMN)
            await client.post(
                f"{BALANCE_SERVICE_URL}/balance/check",
                json={"user_id": req.user_id, "amount": req.amount}
            )
            
            # 4. Llamar a Banco B (Paso 10 del BPMN)
            response_bank_b = await client.post(
                f"{MOCK_BANKB_URL}/receive",
                json={"from_user": req.user_id, "amount": req.amount, "to_account": req.to_account}
            )
            response_bank_b.raise_for_status() # Falla si Banco B da error

            # 5. Debitar Saldo (Paso 14 del BPMN)
            await client.post(
                f"{BALANCE_SERVICE_URL}/balance/debit",
                json={"user_id": req.user_id, "amount": req.amount}
            )
            
        except httpx.HTTPStatusError as e:
            # Manejar errores de nuestros servicios o del Banco B
            status_code = e.response.status_code
            detail = e.response.json().get("detail", "Error desconocido")
            
            if status_code == 400: # Ej. Fondos insuficientes
                status_final = "FAILED_FUNDS"
            elif status_code == 404: # Ej. Cuenta no encontrada
                status_final = "FAILED_ACCOUNT"
            else: # Ej. Banco B caído
                status_final = "FAILED_REMOTE"
                
            db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s", (status_final, datetime.now(timezone.utc), tx_id))
            logger.warning(f"Transferencia {status_final} para tx {tx_id}: {detail}")
            raise HTTPException(status_code=status_code, detail=detail)
        
        except httpx.RequestError as e:
            # Error de red (ej. Balance Service o Banco B caídos)
            db.execute(f"UPDATE {KEYSPACE}.transactions SET status = 'FAILED_NETWORK', updated_at = %s WHERE id = %s", (datetime.now(timezone.utc), tx_id))
            logger.error(f"Fallo de red en tx {tx_id}: {e}")
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Error de red: {e}")

    # 6. Todo OK: Marcar como COMPLETED (Paso 13 del BPMN)
    db.execute(f"INSERT INTO {KEYSPACE}.idempotency_keys (key, transaction_id) VALUES (%s, %s)", (uuid.UUID(idempotency_key), tx_id))
    db.execute(f"UPDATE {KEYSPACE}.transactions SET status = 'COMPLETED', updated_at = %s WHERE id = %s", (datetime.now(timezone.utc), tx_id))

    logger.info(f"Transferencia COMPLETED para user_id {req.user_id}, tx_id {tx_id}")
    tx = db.execute(f"SELECT * FROM {KEYSPACE}.transactions WHERE id = %s", (tx_id,)).one()
    return tx