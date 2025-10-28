"""Servicio FastAPI para gestionar el registro de transacciones (Ledger) en Cassandra."""

import os
import httpx
import uuid
import json
import logging
from datetime import datetime, timezone
from typing import Optional # Añadido para Optional

from fastapi import FastAPI, Depends, HTTPException, status, Header, Request
from cassandra.cluster import Session
from cassandra.query import SimpleStatement
from fastapi.responses import Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
import time

# Importaciones locales (absolutas)
import cassandra_db # Módulo para conexión y schema
import schemas       # Módulo con modelos Pydantic
# Importamos directamente la función load_env_vars si utils.py existe
try:
    from utils import load_env_vars
    load_env_vars() # Carga y verifica variables de entorno
except ImportError:
    # Si utils.py no existe, cargamos directamente y asumimos que las variables existen
    from dotenv import load_dotenv
    load_dotenv()
    logger = logging.getLogger(__name__) # Necesitamos el logger aquí si utils no existe
    logger.warning("Archivo utils.py no encontrado, cargando .env directamente.")
    # Verificar manualmente si faltan variables esenciales podría ser necesario aquí


# Configuración leída desde el entorno (después de load_env_vars o load_dotenv)
BALANCE_SERVICE_URL = os.getenv("BALANCE_SERVICE_URL")
INTERBANK_SERVICE_URL = os.getenv("INTERBANK_SERVICE_URL")
INTERBANK_API_KEY = os.getenv("INTERBANK_API_KEY", "dummy-key-for-dev") # Clave para hablar con otros bancos
KEYSPACE = cassandra_db.KEYSPACE

# Configura logger (asegurar que se configure bien incluso si utils.py falta)
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
            db_session = None # Marcar como no disponible
            # Idealmente: Salir o entrar en modo de error
    else:
        logger.critical("FATAL: No se pudo conectar a Cassandra al inicio. El servicio no funcionará.")
        # Idealmente: Salir o entrar en modo de error

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
REQUEST_COUNT = Counter(
    "ledger_requests_total",
    "Total requests processed by Ledger Service",
    ["method", "endpoint", "status_code"]
)
REQUEST_LATENCY = Histogram(
    "ledger_request_latency_seconds",
    "Request latency in seconds for Ledger Service",
    ["endpoint"]
)
DEPOSIT_COUNT = Counter(
    "ledger_deposits_total",
    "Número total de depósitos procesados exitosamente"
)
TRANSFER_COUNT = Counter(
    "ledger_transfers_total",
    "Número total de transferencias procesadas exitosamente"
)
CONTRIBUTION_COUNT = Counter(
    "ledger_contributions_total",
    "Número total de aportes a grupos procesados exitosamente"
)

# --- Middleware para Métricas ---
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start_time = time.time()
    response = None
    status_code = 500 # Default

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
        # Normalizar si es necesario
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
    """ Verifica si una clave de idempotencia (UUID) ya existe. Devuelve el tx_id si existe."""
    if not key: # Si no se proporciona la cabecera
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
     """Obtiene una transacción por su ID desde Cassandra."""
     try:
          query = SimpleStatement(f"SELECT * FROM {KEYSPACE}.transactions WHERE id = %s")
          result = session.execute(query, (tx_id,)).one()
          return result._asdict() if result else None # Convertir Row a dict
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
        # Si la key existe pero no la tx, hay inconsistencia
        logger.error(f"INCONSISTENCIA: Key {idempotency_key} existe pero tx_id {existing_tx_id} no encontrado.")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error de idempotencia: Transacción original no encontrada")

    tx_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    metadata_json = json.dumps({"description": "Depósito en BDI"})
    status_final = "PENDING" # Estado inicial

    # Registrar Intento (PENDING)
    try:
        db.execute(
            f"""
            INSERT INTO {KEYSPACE}.transactions (
                id, user_id, source_wallet_type, source_wallet_id,
                destination_wallet_type, destination_wallet_id, type, amount, currency,
                status, created_at, updated_at, metadata
            ) VALUES (%s, %s, 'EXTERNAL', 'N/A', 'BDI', %s, 'DEPOSIT', %s, 'PEN', %s, %s, %s, %s)
            """,
            (tx_id, req.user_id, str(req.user_id), req.amount, status_final, now, now, metadata_json)
        )
    except Exception as e:
        logger.error(f"Error al insertar tx PENDING (depósito) {tx_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error al registrar la transacción inicial")

    # Llamar a Balance Service para acreditar
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BALANCE_SERVICE_URL}/balance/credit",
                json={"user_id": req.user_id, "amount": req.amount}
            )
            response.raise_for_status() # Lanza error si balance_service falla (4xx, 5xx)
        status_final = "COMPLETED" # Marcamos como completado si la llamada fue exitosa
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        status_final = "FAILED_BALANCE_SVC"
        detail = f"Balance Service falló al acreditar: {e}"
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        if isinstance(e, httpx.HTTPStatusError):
            detail = e.response.json().get("detail", str(e))
            status_code = e.response.status_code # Usamos el código de error de balance_service

        logger.error(f"Fallo en tx {tx_id} (depósito): {detail}")
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                   (status_final, datetime.now(timezone.utc), tx_id))
        raise HTTPException(status_code=status_code, detail=detail)

    # Si todo fue bien, registramos idempotencia y actualizamos estado final
    try:
        idempotency_uuid = uuid.UUID(idempotency_key)
        db.execute(f"INSERT INTO {KEYSPACE}.idempotency_keys (key, transaction_id) VALUES (%s, %s)",
                   (idempotency_uuid, tx_id))
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                   (status_final, datetime.now(timezone.utc), tx_id))
        DEPOSIT_COUNT.inc() # Incrementamos métrica
        logger.info(f"Depósito {status_final} para user_id {req.user_id}, tx_id {tx_id}")
    except Exception as final_e:
        # Error MUY RARO: Fallo después de acreditar pero antes de marcar completed/idempotencia
        status_final = "PENDING_CONFIRMATION" # Estado especial para reconciliación
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                   (status_final, datetime.now(timezone.utc), tx_id))
        logger.critical(f"¡FALLO CRÍTICO post-crédito en tx {tx_id}! Estado: {status_final}. Error: {final_e}. Requiere reconciliación manual.")
        # Devolvemos la transacción en este estado ambiguo

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
    # Validamos banco destino (podría hacerse con un Enum o config)
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
    # Guardamos el número de teléfono en metadata por claridad
    metadata = {"to_bank": req.to_bank, "destination_phone_number": req.destination_phone_number}
    status_final = "PENDING"
    currency = "PEN" # Asumir PEN por defecto o obtener de config/request

    # Registrar Intento (PENDING)
    try:
        db.execute(
            f"""
            INSERT INTO {KEYSPACE}.transactions (
                id, user_id, source_wallet_type, source_wallet_id,
                destination_wallet_type, destination_wallet_id, type, amount, currency,
                status, created_at, updated_at, metadata
            ) VALUES (%s, %s, 'BDI', %s, 'EXTERNAL_BANK', %s, 'TRANSFER', %s, %s, %s, %s, %s, %s)
            """,
            (tx_id, req.user_id, str(req.user_id), req.destination_phone_number, # Origen=BDI, Destino=Nro Celular
             req.amount, currency, status_final, now, now, json.dumps(metadata))
        )
    except Exception as e:
        logger.error(f"Error al insertar tx PENDING (transfer) {tx_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error al registrar la transacción inicial")

    # Flujo de la transacción externa
    try:
        async with httpx.AsyncClient(timeout=15.0) as client: # Timeout para llamadas externas
            # 1. Verificar Fondos en BDI origen
            check_res = await client.post(
                f"{BALANCE_SERVICE_URL}/balance/check",
                json={"user_id": req.user_id, "amount": req.amount}
            )
            check_res.raise_for_status() # Falla si 4xx/5xx (ej. 400 Fondos Insuficientes)

            # 2. Llamar al Servicio Interbancario
            interbank_payload = {
                "origin_bank": "PIXEL_MONEY",
                "origin_account_id": str(req.user_id),
                "destination_bank": req.to_bank.upper(), # HAPPY_MONEY
                "destination_phone_number": req.destination_phone_number,
                "amount": req.amount,
                "currency": currency,
                "transaction_id": str(tx_id),
                "description": f"Transferencia desde Pixel Money"
            }
            interbank_headers = {"X-API-KEY": INTERBANK_API_KEY}

            response_bank_b = await client.post(
                f"{INTERBANK_SERVICE_URL}/interbank/transfers",
                json=interbank_payload,
                headers=interbank_headers
            )

            # Si el banco externo rechaza (4xx) o falla (5xx)
            if response_bank_b.status_code >= 400:
                 try:
                     bank_b_error = response_bank_b.json()
                     # Usamos el código de error del banco externo si está disponible
                     status_final = bank_b_error.get("error_code", f"FAILED_REMOTE_{response_bank_b.status_code}")
                     detail = bank_b_error.get("message", f"Banco externo devolvió error {response_bank_b.status_code}")
                 except json.JSONDecodeError:
                     status_final = f"FAILED_REMOTE_{response_bank_b.status_code}"
                     detail = f"Banco externo devolvió error {response_bank_b.status_code} sin cuerpo JSON válido."
                 logger.warning(f"Transferencia {status_final} para tx {tx_id}: {detail}")
                 raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) # Devolvemos 400 al cliente

            # Si el banco externo acepta (2xx)
            bank_b_response = response_bank_b.json()
            remote_tx_id = bank_b_response.get("remote_transaction_id")
            metadata["remote_tx_id"] = remote_tx_id # Guardamos ID externo en metadata
            logger.info(f"Banco externo aceptó tx {tx_id}. ID remoto: {remote_tx_id}")

            # 3. Debitar Saldo en BDI origen (¡Solo si Banco B aceptó!)
            debit_res = await client.post(
                f"{BALANCE_SERVICE_URL}/balance/debit",
                json={"user_id": req.user_id, "amount": req.amount}
            )
            # Si el débito falla DESPUÉS de que Banco B aceptó, es un problema grave.
            if debit_res.status_code >= 400:
                status_final = "FAILED_DEBIT_POST_CONFIRMATION" # Estado crítico
                detail = f"Banco externo confirmó, pero falló el débito local: {debit_res.json().get('detail', 'Error desconocido')}"
                logger.critical(f"¡FALLO CRÍTICO en tx {tx_id}! {detail}. Requiere reconciliación manual.")
                # Actualizamos estado pero NO lanzamos excepción para guardar idempotencia y estado crítico
                db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, metadata = %s, updated_at = %s WHERE id = %s",
                           (status_final, json.dumps(metadata), datetime.now(timezone.utc), tx_id))
                # Devolvemos error 500 al cliente indicando fallo interno grave
                raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Fallo crítico post-confirmación externa.")

            # 4. Todo OK: Marcar como COMPLETED
            status_final = "COMPLETED"

    except HTTPException as http_exc:
        # Errores controlados (ej. 400 Fondos Insuficientes del check inicial, 4xx devuelto por Banco B)
        # El estado final ya debería estar asignado (FAILED_FUNDS, FAILED_REMOTE_XXX)
        # Actualizamos la transacción en Cassandra con el estado final
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, metadata = %s, updated_at = %s WHERE id = %s",
                   (status_final, json.dumps(metadata), datetime.now(timezone.utc), tx_id))
        # Re-lanzamos la excepción para que el cliente reciba el código y detalle correctos
        raise http_exc

    except httpx.RequestError as e:
        status_final = "FAILED_NETWORK"
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, metadata = %s, updated_at = %s WHERE id = %s",
                   (status_final, json.dumps(metadata), datetime.now(timezone.utc), tx_id))
        logger.error(f"Fallo de red en tx {tx_id} (transferencia): {e}", exc_info=True)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Error de red al contactar servicios: {e}")

    except Exception as e:
         status_final = "FAILED_UNKNOWN"
         db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, metadata = %s, updated_at = %s WHERE id = %s",
                   (status_final, json.dumps(metadata), datetime.now(timezone.utc), tx_id))
         logger.error(f"Error inesperado en tx {tx_id} (transferencia): {e}", exc_info=True)
         raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno inesperado procesando la transferencia")

    # Si llegamos aquí, la transacción fue (o debería haber sido) exitosa
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
             # Error MUY RARO y CRÍTICO
             status_final = "PENDING_CONFIRMATION"
             db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, metadata = %s, updated_at = %s WHERE id = %s",
                   (status_final, json.dumps(metadata), datetime.now(timezone.utc), tx_id))
             logger.critical(f"¡FALLO CRÍTICO post-débito/confirmación externa en tx {tx_id}! Estado: {status_final}. Error: {final_e}. Requiere reconciliación.")
             # No relanzamos, devolvemos estado ambiguo

    # Devolvemos el estado final de la transacción recuperándola de Cassandra
    tx_data = await get_transaction_by_id(db, tx_id)
    if not tx_data: raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "No se pudo recuperar la transacción final")
    return schemas.Transaction(**tx_data)


@app.post("/contribute", response_model=schemas.Transaction, status_code=status.HTTP_201_CREATED, tags=["Transactions"])
async def contribute_to_group(
    req: schemas.ContributionRequest,
    idempotency_key: Optional[str] = Header(None, description="Clave única (UUID v4) para idempotencia"),
    db: Session = Depends(get_db)
):
    """Procesa un aporte desde una BDI a una BDG."""
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
    currency = "PEN" # Asumir PEN

    # Registrar Intento (PENDING)
    try:
        db.execute(
            f"""
            INSERT INTO {KEYSPACE}.transactions (
                id, user_id, source_wallet_type, source_wallet_id,
                destination_wallet_type, destination_wallet_id, type, amount, currency,
                status, created_at, updated_at, metadata
            ) VALUES (%s, %s, 'BDI', %s, 'BDG', %s, 'CONTRIBUTION', %s, %s, %s, %s, %s, %s)
            """,
            (tx_id, req.user_id, str(req.user_id), str(req.group_id), # Origen=BDI, Destino=BDG
             req.amount, currency, status_final, now, now, json.dumps(metadata))
        )
    except Exception as e:
        logger.error(f"Error al insertar tx PENDING (aporte) {tx_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error al registrar la transacción inicial")

    # Flujo del aporte: Check -> Debit BDI -> Credit BDG
    reverted_debit = False # Bandera para saber si tuvimos que revertir
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 1. Verificar Fondos en BDI origen
            await client.post(
                f"{BALANCE_SERVICE_URL}/balance/check",
                json={"user_id": req.user_id, "amount": req.amount}
            )

            # 2. Debitar BDI origen
            await client.post(
                f"{BALANCE_SERVICE_URL}/balance/debit",
                json={"user_id": req.user_id, "amount": req.amount}
            )

            # 3. Acreditar BDG destino
            await client.post(
                f"{BALANCE_SERVICE_URL}/group_balance/credit",
                json={"group_id": req.group_id, "amount": req.amount}
            )

            # 4. Todo OK
            status_final = "COMPLETED"

    except HTTPException as http_exc: # Capturamos excepciones de nuestros endpoints
        status_code = http_exc.status_code
        detail = http_exc.detail
        if status_code == 400: status_final = "FAILED_FUNDS"
        elif status_code == 404: status_final = "FAILED_ACCOUNT" # BDI o BDG no encontrada
        else: status_final = "FAILED_BALANCE_SVC" # Otro error del balance service

        # Lógica de Reversión: Si falló DESPUÉS del débito, intentamos revertir.
        # Asumimos que el débito fue el paso anterior al fallo.
        if status_final != "FAILED_FUNDS": # No revertimos si falló por fondos insuficientes
            logger.warning(f"Aporte falló ({status_final}) después del débito en tx {tx_id}. Intentando revertir débito BDI...")
            try:
                async with httpx.AsyncClient() as revert_client:
                    revert_res = await revert_client.post(
                        f"{BALANCE_SERVICE_URL}/balance/credit", # Usamos CREDIT para revertir
                        json={"user_id": req.user_id, "amount": req.amount}
                    )
                    revert_res.raise_for_status()
                reverted_debit = True
                status_final += "_REVERTED"
                logger.info(f"Reversión de débito BDI para tx {tx_id} exitosa.")
            except Exception as revert_e:
                logger.critical(f"¡FALLO CRÍTICO EN REVERSIÓN para tx {tx_id}! Error: {revert_e}. Requiere reconciliación manual.")
                status_final += "_REVERT_FAILED"

        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                   (status_final, datetime.now(timezone.utc), tx_id))
        # Relanzamos la excepción original para el cliente
        raise HTTPException(status_code=status_code, detail=detail)

    except httpx.RequestError as e:
        status_final = "FAILED_NETWORK"
        db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                   (status_final, datetime.now(timezone.utc), tx_id))
        logger.error(f"Fallo de red en tx {tx_id} (aporte): {e}", exc_info=True)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Error de red con Balance Service: {e}")

    except Exception as e:
         status_final = "FAILED_UNKNOWN"
         db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                   (status_final, datetime.now(timezone.utc), tx_id))
         logger.error(f"Error inesperado en tx {tx_id} (aporte): {e}", exc_info=True)
         raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno inesperado procesando el aporte")

    # Si llegamos aquí, fue exitoso
    if status_final == "COMPLETED":
        try:
            idempotency_uuid = uuid.UUID(idempotency_key)
            db.execute(f"INSERT INTO {KEYSPACE}.idempotency_keys (key, transaction_id) VALUES (%s, %s)",
                       (idempotency_uuid, tx_id))
            db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                       (status_final, datetime.now(timezone.utc), tx_id))
            CONTRIBUTION_COUNT.inc() # Incrementamos métrica
            logger.info(f"Aporte {status_final} de user_id {req.user_id} a group_id {req.group_id}, tx_id {tx_id}")
        except Exception as final_e:
             status_final = "PENDING_CONFIRMATION"
             db.execute(f"UPDATE {KEYSPACE}.transactions SET status = %s, updated_at = %s WHERE id = %s",
                   (status_final, datetime.now(timezone.utc), tx_id))
             logger.critical(f"¡FALLO CRÍTICO post-aporte en tx {tx_id}! Estado: {status_final}. Error: {final_e}. Requiere reconciliación.")

    # Devolvemos el estado final de la transacción
    tx_data = await get_transaction_by_id(db, tx_id)
    if not tx_data: raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "No se pudo recuperar la transacción final")
    return schemas.Transaction(**tx_data)


# --- Endpoint de Salud y Métricas ---
@app.get("/health", tags=["Monitoring"])
def health_check():
    """Verifica la salud básica del servicio y la conexión a Cassandra."""
    db_status = "ok"
    try:
        if db_session:
            # Ejecuta una consulta simple para verificar la conexión
            db_session.execute("SELECT now() FROM system.local", timeout=2.0)
        else:
            db_status = "error - session not initialized"
    except Exception as e:
        logger.error(f"Health check fallido - Error de Cassandra: {e}", exc_info=True)
        db_status = "error"
        # Devolver 503 si Cassandra no está disponible
        # raise HTTPException(status_code=503, detail="Database (Cassandra) connection error")

    return {"status": "ok", "service": "ledger_service", "database": db_status}

@app.get("/metrics", tags=["Monitoring"])
def metrics():
    """Expone métricas de la aplicación para Prometheus."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)