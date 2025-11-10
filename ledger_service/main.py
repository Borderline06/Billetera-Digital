"""Servicio FastAPI para gestionar el registro de transacciones (Ledger) en Cassandra."""

import os
import httpx
import uuid
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from collections import defaultdict
from decimal import Decimal

from fastapi import FastAPI, Depends, HTTPException, status, Header, Request, Response
from cassandra.cluster import Session
from cassandra.query import SimpleStatement, BatchStatement
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
import time

# Importaciones locales (absolutas)
import cassandra_db
import schemas


try:
    from utils import load_env_vars
    load_env_vars() # Carga y verifica variables de entorno
except ImportError:
    from dotenv import load_dotenv
    load_dotenv()
    if 'logger' not in locals(): # Configura el logger si utils.py no lo hizo
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)
    logger.warning("Archivo utils.py no encontrado, cargando .env directamente.")

# Configuración leída desde el entorno
BALANCE_SERVICE_URL = os.getenv("BALANCE_SERVICE_URL")
INTERBANK_SERVICE_URL = os.getenv("INTERBANK_SERVICE_URL")
INTERBANK_API_KEY = os.getenv("INTERBANK_API_KEY", "dummy-key-for-dev")
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL")
KEYSPACE = cassandra_db.KEYSPACE

# Configura logger (si no se hizo arriba)
if 'logger' not in locals():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)

# --- Inicialización de la App y Base de Datos ---
app = FastAPI(
    title="Ledger Service - Pixel Money",
    description="Registra todas las transacciones financieras (depósitos, transferencias, aportes) en Cassandra.",
    version="1.0.0"
)
db_session: Optional[Session] = None

@app.on_event("startup")
def startup_event():
    """Inicializa la conexión a Cassandra y crea/verifica el schema al arrancar."""
    global db_session
    logger.info("Iniciando Ledger Service...")
    db_session = cassandra_db.get_cassandra_session()
    if db_session:
        try:
            cassandra_db.create_keyspace_and_tables(db_session)
        except Exception as e:
            logger.critical(f"FATAL: Error al configurar schema de Cassandra: {e}. El servicio no funcionará.", exc_info=True)
            db_session = None 
    else:
        logger.critical("FATAL: No se pudo conectar a Cassandra al inicio. El servicio no funcionará.")

@app.on_event("shutdown")
def shutdown_event():
    """Cierra la conexión a Cassandra al apagar."""
    if db_session and db_session.cluster:
        db_session.cluster.shutdown()
        logger.info("Conexión a Cassandra cerrada.")

def get_db() -> Session:
    """Función de dependencia de FastAPI para obtener la sesión de Cassandra."""
    if db_session is None:
        logger.error("Intento de acceso a BD fallido: Sesión de Cassandra no disponible.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servicio de base de datos (Cassandra) no disponible temporalmente."
        )
    return db_session

# --- Métricas Prometheus ---
REQUEST_COUNT = Counter("ledger_requests_total", "Total requests", ["method", "endpoint", "status_code"])
REQUEST_LATENCY = Histogram("ledger_request_latency_seconds", "Request latency", ["endpoint"])
DEPOSIT_COUNT = Counter("ledger_deposits_total", "Número total de depósitos procesados")
TRANSFER_COUNT = Counter("ledger_transfers_total", "Número total de transferencias procesadas")
CONTRIBUTION_COUNT = Counter("ledger_contributions_total", "Número total de aportes a grupos")
LEDGER_P2P_TRANSFERS_TOTAL = Counter(
    "ledger_p2p_transfers_total",
    "Total de transferencias P2P (BDI -> BDI) procesadas"
)

# --- Middleware para Métricas ---
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start_time = time.time()
    response = None
    status_code = 500

    try:
        response = await call_next(request)
        status_code = response.status_code
    except HTTPException as http_exc:
        status_code = http_exc.status_code
        raise http_exc
    except Exception as exc:
        logger.error(f"Middleware error: {exc}", exc_info=True)
        return Response("Internal Server Error", status_code=500)
    finally:
        latency = time.time() - start_time
        endpoint = request.url.path
        final_status_code = getattr(response, 'status_code', status_code)
        REQUEST_LATENCY.labels(endpoint=endpoint).observe(latency)
        REQUEST_COUNT.labels(
            method=request.method,
            endpoint=endpoint,
            status_code=final_status_code
        ).inc()

    return response

# --- Función de Seguridad: Idempotencia ---
def check_idempotency(session: Session, key: str) -> Optional[uuid.UUID]:
    """Verifica si una clave de idempotencia (UUID) ya existe. Devuelve el tx_id si existe."""
    if not key:
        return None
    try:
        key_uuid = uuid.UUID(key)
        query = SimpleStatement(f"SELECT transaction_id FROM {KEYSPACE}.idempotency_keys WHERE key = %s")
        result = session.execute(query, (key_uuid,)).one()
        return result.transaction_id if result else None
    except (ValueError, TypeError):
        logger.warning(f"Clave de idempotencia inválida recibida: {key}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Formato de Idempotency-Key inválido (debe ser UUID)")
    except Exception as e:
         logger.error(f"Error al verificar idempotencia para key {key}: {e}", exc_info=True)
         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error interno al verificar idempotencia")


async def get_transaction_by_id(session: Session, tx_id: uuid.UUID) -> Optional[dict]:
     """Obtiene una transacción por su ID desde Cassandra y la convierte a dict."""
     try:
          query = SimpleStatement(f"SELECT * FROM {KEYSPACE}.transactions WHERE id = %s")
          result = session.execute(query, (tx_id,)).one()
          return result._asdict() if result else None
     except Exception as e:
          logger.error(f"Error al obtener transacción {tx_id}: {e}", exc_info=True)
          return None

# --- Endpoints de la API ---

@app.post("/deposit", response_model=schemas.Transaction, status_code=status.HTTP_201_CREATED, tags=["Transactions"])
async def deposit(
    req: schemas.DepositRequest,
    idempotency_key: Optional[str] = Header(None, description="Clave única (UUID v4) para idempotencia"),
    db: Session = Depends(get_db)
):
    """Procesa un depósito en una cuenta individual (BDI)."""
    if idempotency_key is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cabecera Idempotency-Key es requerida")

    existing_tx_id = check_idempotency(db, idempotency_key)
    if existing_tx_id:
        logger.info(f"Depósito duplicado detectado (Idempotency Key: {idempotency_key}). Devolviendo tx original: {existing_tx_id}")
        tx_data = await get_transaction_by_id(db, existing_tx_id)
        if tx_data: return schemas.Transaction(**tx_data)
        logger.error(f"INCONSISTENCIA: Key {idempotency_key} existe pero tx_id {existing_tx_id} no encontrado.")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error de idempotencia: Transacción original no encontrada")

    tx_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    metadata_json = json.dumps({"description": "Depósito en BDI"})
    status_final = "PENDING"
    currency = "PEN"

   
    try:
        # Preparamos las dos consultas (para 'transactions' y 'transactions_by_user')
        query_by_id = SimpleStatement(f"""
            INSERT INTO {KEYSPACE}.transactions (
                id, user_id, source_wallet_type, source_wallet_id,
                destination_wallet_type, destination_wallet_id, type, amount, currency,
                status, created_at, updated_at, metadata
            ) VALUES (%s, %s, 'EXTERNAL', 'N/A', 'BDI', %s, 'DEPOSIT', %s, %s, %s, %s, %s, %s)
        """)

        query_by_user = SimpleStatement(f"""
            INSERT INTO {KEYSPACE}.transactions_by_user (
                user_id, created_at, id, source_wallet_type, source_wallet_id,
                destination_wallet_type, destination_wallet_id, type, amount, currency,
                status, updated_at, metadata
            ) VALUES (%s, %s, %s, 'EXTERNAL', 'N/A', 'BDI', %s, 'DEPOSIT', %s, %s, %s, %s, %s)
        """)

        # Usamos un BATCH para asegurar que ambas escrituras ocurran o ninguna
        batch = BatchStatement()
        batch.add(query_by_id, (tx_id, req.user_id, str(req.user_id), req.amount, currency, status_final, now, now, metadata_json))
        batch.add(query_by_user, (req.user_id, now, tx_id, str(req.user_id), req.amount, currency, status_final, now, metadata_json))

        db.execute(batch)

    except Exception as e:
        logger.error(f"Error al insertar BATCH PENDING (depósito) {tx_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error al registrar la transacción inicial")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BALANCE_SERVICE_URL}/balance/credit",
                json={"user_id": req.user_id, "amount": req.amount}
            )
            response.raise_for_status()
        status_final = "COMPLETED"
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        status_final = "FAILED_BALANCE_SVC"
        detail = f"Balance Service falló al acreditar: {e}"
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        if isinstance(e, httpx.HTTPStatusError):
            try:
                detail = e.response.json().get("detail", str(e))
            except json.JSONDecodeError:
                detail = e.response.text
            status_code = e.response.status_code

        logger.error(f"Fallo en tx {tx_id} (depósito): {detail}")
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                   (status_final, datetime.now(timezone.utc), tx_id))
        raise HTTPException(status_code=status_code, detail=detail)

    try:
        idempotency_uuid = uuid.UUID(idempotency_key)
        db.execute(f"INSERT INTO {KEYSPACE}.idempotency_keys (key, transaction_id) VALUES (%s, %s)",
                   (idempotency_uuid, tx_id))
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                   (status_final, datetime.now(timezone.utc), tx_id))
        DEPOSIT_COUNT.inc()
        logger.info(f"Depósito {status_final} para user_id {req.user_id}, tx_id {tx_id}")
    except Exception as final_e:
        status_final = "PENDING_CONFIRMATION"
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                   (status_final, datetime.now(timezone.utc), tx_id))
        logger.critical(f"¡FALLO CRÍTICO post-crédito en tx {tx_id}! Estado: {status_final}. Error: {final_e}. Requiere reconciliación manual.")

    tx_data = await get_transaction_by_id(db, tx_id)
    if not tx_data: raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "No se pudo recuperar la transacción final")
    return schemas.Transaction(**tx_data)


@app.post("/transfer", response_model=schemas.Transaction, status_code=status.HTTP_201_CREATED, tags=["Transactions"])
async def transfer(
    req: schemas.TransferRequest, 
    idempotency_key: Optional[str] = Header(None, description="Clave única (UUID v4) para idempotencia"),
    db: Session = Depends(get_db)
):
    """Procesa una transferencia BDI -> BDI (Externa a Happy Money)."""
    if idempotency_key is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cabecera Idempotency-Key es requerida")
    if req.to_bank.upper() != "HAPPY_MONEY":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Banco de destino '{req.to_bank}' no soportado")

    existing_tx_id = check_idempotency(db, idempotency_key)
    if existing_tx_id:
        logger.info(f"Transferencia duplicada detectada (Key: {idempotency_key}). Devolviendo tx: {existing_tx_id}")
        tx_data = await get_transaction_by_id(db, existing_tx_id)
        if tx_data: return schemas.Transaction(**tx_data)
        logger.error(f"INCONSISTENCIA: Key {idempotency_key} existe pero tx_id {existing_tx_id} no encontrado.")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error de idempotencia: Transacción original no encontrada")

    tx_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    
    metadata = {"to_bank": req.to_bank, "destination_phone_number": req.destination_phone_number}
    status_final = "PENDING"
    currency = "PEN"

        # En la función transfer(), reemplaza el primer 'try...'
    try:
        query_by_id = SimpleStatement(f"""
            INSERT INTO {KEYSPACE}.transactions (
                id, user_id, source_wallet_type, source_wallet_id,
                destination_wallet_type, destination_wallet_id, type, amount, currency,
                status, created_at, updated_at, metadata
            ) VALUES (%s, %s, 'BDI', %s, 'EXTERNAL_BANK', %s, 'TRANSFER', %s, %s, %s, %s, %s, %s)
        """)

        query_by_user = SimpleStatement(f"""
            INSERT INTO {KEYSPACE}.transactions_by_user (
                user_id, created_at, id, source_wallet_type, source_wallet_id,
                destination_wallet_type, destination_wallet_id, type, amount, currency,
                status, updated_at, metadata
            ) VALUES (%s, %s, %s, 'BDI', %s, 'EXTERNAL_BANK', %s, 'TRANSFER', %s, %s, %s, %s, %s)
        """)

        batch = BatchStatement()
        batch.add(query_by_id, (tx_id, req.user_id, str(req.user_id), req.destination_phone_number, req.amount, currency, status_final, now, now, json.dumps(metadata)))
        batch.add(query_by_user, (req.user_id, now, tx_id, str(req.user_id), req.destination_phone_number, req.amount, currency, status_final, now, json.dumps(metadata)))

        db.execute(batch)

    except Exception as e:
        logger.error(f"Error al insertar BATCH PENDING (transfer) {tx_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error al registrar la transacción inicial")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # 1. Verificar Fondos en BDI origen
            logger.debug(f"Tx {tx_id}: Verificando fondos para user_id {req.user_id}")
            check_res = await client.post(
                f"{BALANCE_SERVICE_URL}/balance/check",
                json={"user_id": req.user_id, "amount": req.amount}
            )
            # ¡Si esto falla (400), saltará al 'except HTTPStatusError'
            check_res.raise_for_status() 
            logger.debug(f"Tx {tx_id}: Fondos verificados.")

            # 2. Llamar al Servicio Interbancario (Happy Money)
            logger.debug(f"Tx {tx_id}: Llamando a Interbank Service...")
            interbank_payload = {
                "origin_bank": "PIXEL_MONEY",
                "origin_account_id": str(req.user_id),
                "destination_bank": req.to_bank.upper(),
                "destination_phone_number": req.destination_phone_number,
                "amount": req.amount,
                "currency": currency,
                "transaction_id": str(tx_id),
                "description": "Transferencia desde Pixel Money"
            }
            interbank_headers = {"X-API-KEY": INTERBANK_API_KEY}

            response_bank_b = await client.post(
                f"{INTERBANK_SERVICE_URL}/interbank/transfers",
                json=interbank_payload,
                headers=interbank_headers
            )

            # ¡Si el banco externo falla, raise_for_status() también saltará!
            response_bank_b.raise_for_status() 

            bank_b_response = response_bank_b.json()
            remote_tx_id = bank_b_response.get("remote_transaction_id")
            metadata["remote_tx_id"] = remote_tx_id
            logger.info(f"Banco externo aceptó tx {tx_id}. ID remoto: {remote_tx_id}")

            # 3. Debitar Saldo en BDI origen (Paso final)
            logger.debug(f"Tx {tx_id}: Debitando saldo de user_id {req.user_id}")
            debit_res = await client.post(
                f"{BALANCE_SERVICE_URL}/balance/debit",
                json={"user_id": req.user_id, "amount": req.amount}
            )
            debit_res.raise_for_status() # Si el débito falla, saltará

            # 4. Todo OK
            status_final = "COMPLETED"

    # --- INICIO DEL BLOQUE CORREGIDO ---
    except httpx.HTTPStatusError as e:
        
        status_code = e.response.status_code
        try:
            detail = e.response.json().get("detail", "Error desconocido del servicio interno.")
        except json.JSONDecodeError:
            detail = e.response.text

        if status_code == 400: status_final = "FAILED_FUNDS" # Asumimos que 400 es Fondos Insuficientes
        elif status_code == 404: status_final = "FAILED_ACCOUNT"
        else: status_final = f"FAILED_HTTP_{status_code}" # Otro error (ej. 401 de API Key)

        logger.warning(f"Transferencia {status_final} para tx {tx_id}: {detail}")
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, metadata = %s, updated_at = %s WHERE id = %s",
                (status_final, json.dumps(metadata), datetime.now(timezone.utc), tx_id))
        # Re-lanzamos la excepción para que el cliente reciba el código y detalle correctos
        raise HTTPException(status_code=status_code, detail=detail)

    except httpx.RequestError as e: # Error de Red (timeout, servicio caído)
        status_final = "FAILED_NETWORK"
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, metadata = %s, updated_at = %s WHERE id = %s",
                (status_final, json.dumps(metadata), datetime.now(timezone.utc), tx_id))
        logger.error(f"Fallo de red en tx {tx_id} (transferencia): {e}", exc_info=True)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Error de red al contactar servicios: {e}")

    except Exception as e: # Bug nuestro
        status_final = "FAILED_UNKNOWN"
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, metadata = %s, updated_at = %s WHERE id = %s",
                (status_final, json.dumps(metadata), datetime.now(timezone.utc), tx_id))
        logger.error(f"Error inesperado en tx {tx_id} (transferencia): {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno inesperado procesando la transferencia")
    

    # Si todo fue exitoso
    if status_final == "COMPLETED":
        try:
            idempotency_uuid = uuid.UUID(idempotency_key)
            db.execute(f"INSERT INTO {KEYSPACE}.idempotency_keys (key, transaction_id) VALUES (%s, %s)",
                       (idempotency_uuid, tx_id))
            db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, metadata = %s, updated_at = %s WHERE id = %s",
                       (status_final, json.dumps(metadata), datetime.now(timezone.utc), tx_id))
            TRANSFER_COUNT.inc() # Incrementamos métrica
            logger.info(f"Transferencia {status_final} para user_id {req.user_id}, tx_id {tx_id}")
        except Exception as final_e:
             status_final = "PENDING_CONFIRMATION"
             db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, metadata = %s, updated_at = %s WHERE id = %s",
                   (status_final, json.dumps(metadata), datetime.now(timezone.utc), tx_id))
             logger.critical(f"¡FALLO CRÍTICO post-débito en tx {tx_id}! Estado: {status_final}. Error: {final_e}. Requiere reconciliación.")

    tx_data = await get_transaction_by_id(db, tx_id)
    if not tx_data: raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "No se pudo recuperar la transacción final")
    return schemas.Transaction(**tx_data)


@app.post("/contribute", response_model=schemas.Transaction, status_code=status.HTTP_201_CREATED, tags=["Transactions"])
async def contribute_to_group(
    req: schemas.ContributionRequest,
    idempotency_key: Optional[str] = Header(None, description="Clave única (UUID v4) para idempotencia"),
    db: Session = Depends(get_db)
):
    """Procesa un aporte desde una BDI (individual) a una BDG (grupal)."""
    if idempotency_key is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cabecera Idempotency-Key es requerida")

    existing_tx_id = check_idempotency(db, idempotency_key)
    if existing_tx_id:
        logger.info(f"Aporte duplicado detectado (Key: {idempotency_key}). Devolviendo tx: {existing_tx_id}")
        tx_data = await get_transaction_by_id(db, existing_tx_id)
        if tx_data: return schemas.Transaction(**tx_data)
        logger.error(f"INCONSISTENCIA: Key {idempotency_key} existe pero tx_id {existing_tx_id} no encontrado.")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error de idempotencia: Transacción original no encontrada")

    tx_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    metadata = {"contribution_to_group_id": req.group_id}
    status_final = "PENDING"
    currency = "PEN"

    try:
        db.execute(
            f"""
            INSERT INTO {KEYSPACE}.transactions (
                id, user_id, source_wallet_type, source_wallet_id,
                destination_wallet_type, destination_wallet_id, type, amount, currency,
                status, created_at, updated_at, metadata
            ) VALUES (%s, %s, 'BDI', %s, 'BDG', %s, 'CONTRIBUTION', %s, %s, %s, %s, %s, %s)
            """,
            (tx_id, req.user_id, str(req.user_id), str(req.group_id),
             req.amount, currency, status_final, now, now, json.dumps(metadata))
        )
    except Exception as e:
        logger.error(f"Error al insertar tx PENDING (aporte) {tx_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error al registrar la transacción inicial")

    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 1. Verificar Fondos en BDI origen
            logger.debug(f"Tx {tx_id}: Verificando fondos BDI para user_id {req.user_id}")
            await client.post(
                f"{BALANCE_SERVICE_URL}/balance/check",
                json={"user_id": req.user_id, "amount": req.amount}
            )

            # 2. Debitar BDI origen
            logger.debug(f"Tx {tx_id}: Debitando BDI para user_id {req.user_id}")
            await client.post(
                f"{BALANCE_SERVICE_URL}/balance/debit",
                json={"user_id": req.user_id, "amount": req.amount}
            )

            # 3. Acreditar BDG destino
            logger.debug(f"Tx {tx_id}: Acreditando BDG para group_id {req.group_id}")
            await client.post(
                f"{BALANCE_SERVICE_URL}/group_balance/credit",
                json={"group_id": req.group_id, "amount": req.amount}
            )

            # 4. Todo OK
            status_final = "COMPLETED"

    
    except httpx.HTTPStatusError as e: # Captura errores 4xx/5xx de balance_service
        status_code = e.response.status_code
        try:
            detail = e.response.json().get("detail", "Error desconocido del servicio interno.")
        except json.JSONDecodeError:
            detail = e.response.text

        if status_code == 400: status_final = "FAILED_FUNDS"
        elif status_code == 404: status_final = "FAILED_ACCOUNT"
        else: status_final = "FAILED_BALANCE_SVC"

        # Lógica de Reversión (Saga simple)
        if status_final != "FAILED_FUNDS":
            logger.warning(f"Aporte falló ({status_final}) después del débito en tx {tx_id}. Intentando revertir débito BDI...")
            try:
                async with httpx.AsyncClient() as revert_client:
                    revert_res = await revert_client.post(
                        f"{BALANCE_SERVICE_URL}/balance/credit", # Usamos CREDIT para revertir
                        json={"user_id": req.user_id, "amount": req.amount}
                    )
                    revert_res.raise_for_status()
                status_final += "_REVERTED"
                logger.info(f"Reversión de débito BDI para tx {tx_id} exitosa.")
            except Exception as revert_e:
                logger.critical(f"¡FALLO CRÍTICO EN REVERSIÓN para tx {tx_id}! Error: {revert_e}. Requiere reconciliación manual.")
                status_final += "_REVERT_FAILED"

        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                (status_final, datetime.now(timezone.utc), tx_id))
        raise HTTPException(status_code=status_code, detail=detail) # Re-lanzamos

    except httpx.RequestError as e: # Error de Red
        status_final = "FAILED_NETWORK"
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                (status_final, datetime.now(timezone.utc), tx_id))
        logger.error(f"Fallo de red en tx {tx_id} (aporte): {e}", exc_info=True)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Error de red con Balance Service: {e}")

    except Exception as e: # Bug nuestro
        status_final = "FAILED_UNKNOWN"
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                (status_final, datetime.now(timezone.utc), tx_id))
        logger.error(f"Error inesperado en tx {tx_id} (aporte): {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno inesperado procesando el aporte")
    

    # Si todo fue exitoso
    if status_final == "COMPLETED":
        try:
            idempotency_uuid = uuid.UUID(idempotency_key)
            db.execute(f"INSERT INTO {KEYSPACE}.idempotency_keys (key, transaction_id) VALUES (%s, %s)",
                       (idempotency_uuid, tx_id))
            db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                       (status_final, datetime.now(timezone.utc), tx_id))
            CONTRIBUTION_COUNT.inc() # Incrementa métrica
            logger.info(f"Aporte {status_final} de user_id {req.user_id} a group_id {req.group_id}, tx_id {tx_id}")
        except Exception as final_e:
             status_final = "PENDING_CONFIRMATION"
             db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                   (status_final, datetime.now(timezone.utc), tx_id))
             logger.critical(f"¡FALLO CRÍTICO post-aporte en tx {tx_id}! Estado: {status_final}. Error: {final_e}. Requiere reconciliación.")

    tx_data = await get_transaction_by_id(db, tx_id)
    if not tx_data: raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "No se pudo recuperar la transacción final")
    return schemas.Transaction(**tx_data)



@app.get("/transactions/me", response_model=List[schemas.Transaction], tags=["Ledger"])
async def get_my_transactions(
    x_user_id: int = Header(..., alias="X-User-ID"),
    db: Session = Depends(get_db)
):
    """
    Obtiene el historial de transacciones (movimientos) para el usuario autenticado.
    Lee directamente de la base de datos NoSQL (Cassandra).
    """
    logger.info(f"Obteniendo historial de movimientos para user_id: {x_user_id}")

    # Cassandra 
    query = SimpleStatement(f"""
        SELECT * FROM {KEYSPACE}.transactions_by_user
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT 50
    """)

    try:
        # Ejecutamos la consulta
        result_set = db.execute(query, (x_user_id,))

        # Convertimos las filas de Cassandra a nuestro schema Pydantic
        transactions = [schemas.Transaction(**row._asdict()) for row in result_set]

        return transactions
    except Exception as e:
        logger.error(f"Error al obtener transacciones para user_id {x_user_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error al obtener historial de movimientos.")

@app.get("/analytics/daily_balance/{user_id}", tags=["Analytics"])
async def get_daily_balance(user_id: int, db: Session = Depends(get_db)):
    """
    Devuelve el saldo acumulado diario del usuario en los últimos 30 días.
    Fuente: Cassandra (tabla transactions_by_user)
    Ideal para gráficos de área (histórico de balance).
    """
    logger.info(f"Calculando saldo diario para user_id: {user_id}")

    # --- Rango de 30 días atrás ---
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)

    # --- Consulta a Cassandra ---
    query = SimpleStatement(f"""
        SELECT created_at, type, amount
        FROM {KEYSPACE}.transactions_by_user
        WHERE user_id = %s
        AND created_at >= %s
        ORDER BY created_at ASC
    """)

    try:
        result_set = db.execute(query, (user_id, thirty_days_ago))

        # --- Procesar movimientos ---
        daily_balance = defaultdict(float)
        running_balance = Decimal('0.0')

        for row in result_set:
            tx_date = row.created_at.date()

            # Tipos de transacción que SUMAN
            if row.type in ["DEPOSIT", "P2P_RECEIVED", "CONTRIBUTION_RECEIVED"]:
                running_balance += row.amount

            # Tipos de transacción que RESTAN
            elif row.type in ["P2P_SENT", "CONTRIBUTION_SENT", "WITHDRAWAL"]:
                running_balance -= row.amount

            daily_balance[tx_date] = running_balance

        # --- Completar días faltantes ---
        data = []
        balance = 0.0
        for i in range(30, -1, -1):
            day = (now - timedelta(days=i)).date()
            if day in daily_balance:
                balance = daily_balance[day]
            data.append({
                "date": day.isoformat(),
                "balance": float(round(balance, 2))
            })

        return data

    except Exception as e:
        logger.error(f"Error al calcular balance diario para {user_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al calcular el balance diario.")

@app.post("/transfer/p2p", response_model=schemas.Transaction, status_code=status.HTTP_201_CREATED, tags=["Transactions"])
async def transfer_p2p(
    req: schemas.P2PTransferRequest,
    idempotency_key: Optional[str] = Header(None, description="Clave única (UUID v4) para idempotencia"),
    db: Session = Depends(get_db)
):
    """
    Procesa una transferencia P2P (BDI -> BDI) entre usuarios de Pixel Money.
    Orquesta una SAGA:
    1. Resuelve el celular del destinatario (Auth Service).
    2. Verifica y Debita al remitente (Balance Service).
    3. Acredita al destinatario (Balance Service).
    4. Si falla el crédito, revierte el débito.
    5. Escribe ambas transacciones en Cassandra (Batch).
    """
    if idempotency_key is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cabecera Idempotency-Key es requerida")

    sender_id = req.user_id # Inyectado por el Gateway
    recipient_phone = req.destination_phone_number
    amount = req.amount

    # 0. Evitar auto-transferencias (Requerimiento de negocio)
    # (Necesitamos el celular del sender, pero no lo tenemos. Lo omitimos por ahora)

    # 1. Verificar Idempotencia
    existing_tx_id = check_idempotency(db, idempotency_key)
    if existing_tx_id:
        logger.info(f"Transferencia P2P duplicada (Key: {idempotency_key}). Devolviendo tx: {existing_tx_id}")
        tx_data = await get_transaction_by_id(db, existing_tx_id)
        if tx_data: return schemas.Transaction(**tx_data)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error de idempotencia: Tx original no encontrada")

    tx_id_debit = uuid.uuid4() # ID para la transacción de salida
    tx_id_credit = uuid.uuid4() # ID para la transacción de entrada
    now = datetime.now(timezone.utc)
    currency = "PEN"
    recipient_id = None

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            # --- PASO 1: Resolver Destinatario (AUTH SERVICE) ---
            logger.debug(f"Tx {tx_id_debit}: Buscando destinatario por celular: {recipient_phone}")
            auth_res = await client.get(f"{AUTH_SERVICE_URL}/users/by-phone/{recipient_phone}")
            auth_res.raise_for_status() # Lanza 404 si el usuario no existe

            recipient_id = int(auth_res.json()["id"])
            if recipient_id == sender_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "No puedes transferirte dinero a ti mismo.")

            # --- PASO 2: Verificar y Debitar Remitente (BALANCE SERVICE) ---
            logger.debug(f"Tx {tx_id_debit}: Verificando fondos y debitando a user_id {sender_id}")

            # Verificamos fondos (el 'check' ya está en el 'debit', pero es buena práctica)
            check_res = await client.post(f"{BALANCE_SERVICE_URL}/balance/check", json={"user_id": sender_id, "amount": amount})
            check_res.raise_for_status() # Lanza 400 si no hay fondos

            # Debitamos
            debit_res = await client.post(f"{BALANCE_SERVICE_URL}/balance/debit", json={"user_id": sender_id, "amount": amount})
            debit_res.raise_for_status() # Lanza 400 si falla en la concurrencia

            logger.info(f"Tx {tx_id_debit}: Débito de {amount} a {sender_id} exitoso.")

            # --- PASO 3: Acreditar Destinatario (BALANCE SERVICE) ---
            try:
                logger.debug(f"Tx {tx_id_credit}: Acreditando {amount} a user_id {recipient_id}")
                credit_res = await client.post(f"{BALANCE_SERVICE_URL}/balance/credit", json={"user_id": recipient_id, "amount": amount})
                credit_res.raise_for_status()
                logger.info(f"Tx {tx_id_credit}: Crédito a {recipient_id} exitoso.")

            except Exception as credit_error:
                # ¡FALLO CRÍTICO! El débito se hizo pero el crédito falló.
                # --- INICIO DE REVERSIÓN (SAGA) ---
                logger.error(f"¡FALLO DE SAGA! Tx {tx_id_credit} falló. Revertiendo débito {tx_id_debit} para {sender_id}...")
                try:
                    revert_res = await client.post(f"{BALANCE_SERVICE_URL}/balance/credit", json={"user_id": sender_id, "amount": amount})
                    revert_res.raise_for_status()
                    logger.info(f"Reversión de débito {tx_id_debit} para {sender_id} exitosa.")
                except Exception as revert_error:
                    logger.critical(f"¡¡FALLO CRÍTICO DE REVERSIÓN!! El débito {tx_id_debit} no pudo ser revertido. ¡REQUERIRÁ INTERVENCIÓN MANUAL! Error: {revert_error}")
                
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "El servicio del destinatario falló. La transacción ha sido revertida.")

        except httpx.HTTPStatusError as e:
            # Error de 'check' (400), 'auth' (404), o 'debit' (400)
            status_code = e.response.status_code
            detail = e.response.json().get("detail", "Error en servicios internos.")
            logger.warning(f"Fallo transferencia P2P: {detail} (Status: {status_code})")
            raise HTTPException(status_code=status_code, detail=detail)

        except httpx.RequestError as e:
            logger.error(f"Error de red en transferencia P2P: {e}", exc_info=True)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Error de comunicación entre servicios.")

    # --- PASO 4: Escribir en Cassandra (BATCH) ---
    # Si llegamos aquí, la SAGA (débito y crédito) fue exitosa.
    try:
        batch = BatchStatement()

        # Consulta 1: Transacción de SALIDA (Débito)
        q_debit_by_id = SimpleStatement(f"INSERT INTO {KEYSPACE}.transactions (...) VALUES (...)") # (Abreviado por claridad)
        q_debit_by_user = SimpleStatement(f"INSERT INTO {KEYSPACE}.transactions_by_user (...) VALUES (...)")

        

        # Consulta 2: Transacción de ENTRADA (Crédito)
        q_credit_by_id = SimpleStatement(f"INSERT INTO {KEYSPACE}.transactions (...) VALUES (...)")
        q_credit_by_user = SimpleStatement(f"INSERT INTO {KEYSPACE}.transactions_by_user (...) VALUES (...)")

        

        query_by_id = SimpleStatement(f"INSERT INTO {KEYSPACE}.transactions (id, user_id, source_wallet_type, source_wallet_id, destination_wallet_type, destination_wallet_id, type, amount, currency, status, created_at, updated_at) VALUES (%s, %s, 'BDI', %s, 'BDI', %s, 'P2P_SENT', %s, %s, 'COMPLETED', %s, %s)")
        query_by_user = SimpleStatement(f"INSERT INTO {KEYSPACE}.transactions_by_user (user_id, created_at, id, source_wallet_type, source_wallet_id, destination_wallet_type, destination_wallet_id, type, amount, currency, status, updated_at) VALUES (%s, %s, %s, 'BDI', %s, 'BDI', %s, 'P2P_SENT', %s, %s, 'COMPLETED', %s)")

        batch = BatchStatement()
        batch.add(query_by_id, (tx_id_debit, sender_id, str(sender_id), str(recipient_id), amount, currency, now, now))
        batch.add(query_by_user, (sender_id, now, tx_id_debit, str(sender_id), str(recipient_id), amount, currency, now))

        db.execute(batch)
        db.execute(f"INSERT INTO {KEYSPACE}.idempotency_keys (key, transaction_id) VALUES (%s, %s)", (uuid.UUID(idempotency_key), tx_id_debit))

        LEDGER_P2P_TRANSFERS_TOTAL.inc()

        # Devolvemos solo la transacción del remitente
        tx_data = await get_transaction_by_id(db, tx_id_debit)
        if not tx_data: raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "No se pudo recuperar la transacción final")
        return schemas.Transaction(**tx_data)

    except Exception as e:
        logger.critical(f"¡FALLO CRÍTICO POST-SAGA! Tx {tx_id_debit} y {tx_id_credit} tuvieron éxito pero Cassandra falló: {e}", exc_info=True)
        # No revertimos, ¡el dinero ya se movió! Esto requiere reconciliación manual.
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "La transferencia se completó pero falló al registrarse. Contacte a soporte.")



# --- Endpoint de Salud y Métricas ---

@app.get("/health", tags=["Monitoring"])
def health_check():
    """Verifica la salud básica del servicio y la conexión a Cassandra."""
    db_status = "ok"
    try:
        if db_session:
            
            db_session.execute("SELECT now() FROM system.local", timeout=3.0) 
        else:
            db_status = "error - session not initialized"
            raise HTTPException(status_code=503, detail="Sesión de BD no inicializada")
    except Exception as e:
        logger.error(f"Health check fallido - Error de Cassandra: {e}", exc_info=True)
        db_status = "error"
        # Devolvemos 503 para que el healthcheck de Docker falle
        raise HTTPException(status_code=503, detail=f"Database (Cassandra) connection error: {e}")

    return {"status": "ok", "service": "ledger_service", "database": db_status}

@app.get("/metrics", tags=["Monitoring"])
def metrics():
    """Expone métricas de la aplicación para Prometheus."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)