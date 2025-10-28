"""Servicio FastAPI para gestionar saldos de cuentas individuales (BDI) y grupales (BDG)."""

import logging
import time
from fastapi import FastAPI, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from fastapi.responses import Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# Importaciones locales
from db import engine, Base, get_db, SessionLocal # Importamos SessionLocal para chequeo de salud
from models import Account, GroupAccount
import schemas

# Configura logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Crea tablas si no existen al iniciar
try:
    Base.metadata.create_all(bind=engine)
    logger.info("Tablas de base de datos (accounts, group_accounts) verificadas/creadas.")
except Exception as e:
    logger.error(f"Error al inicializar la base de datos: {e}", exc_info=True)
    # Considerar detener el servicio si la BD no está lista

# Inicializa FastAPI
app = FastAPI(
    title="Balance Service - Pixel Money",
    description="Gestiona los saldos de las billeteras individuales (BDI) y grupales (BDG).",
    version="1.0.0"
)

# --- Métricas Prometheus ---
REQUEST_COUNT = Counter(
    "balance_requests_total",
    "Total requests processed by Balance Service",
    ["method", "endpoint", "status_code"]
)
REQUEST_LATENCY = Histogram(
    "balance_request_latency_seconds",
    "Request latency in seconds for Balance Service",
    ["endpoint"]
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

        # Normalizar endpoints con IDs para métricas
        parts = endpoint.split("/")
        if len(parts) == 3:
            if parts[1] == "balance" and parts[2].isdigit():
                endpoint = "/balance/{user_id}"
            elif parts[1] == "group_balance" and parts[2].isdigit():
                endpoint = "/group_balance/{group_id}"

        final_status_code = getattr(response, 'status_code', status_code)

        REQUEST_LATENCY.labels(endpoint=endpoint).observe(latency)
        REQUEST_COUNT.labels(
            method=request.method,
            endpoint=endpoint,
            status_code=final_status_code
        ).inc()

    return response

# --- Endpoints de Salud y Métricas ---
@app.get("/metrics", tags=["Monitoring"])
def metrics():
    """Expone métricas de la aplicación para Prometheus."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health", tags=["Monitoring"])
def health_check():
    """Verifica la salud básica del servicio y la conexión a la BD."""
    db_status = "ok"
    try:
        db = SessionLocal()
        # Ejecuta una consulta simple para verificar la conexión
        db.execute(text("SELECT 1"))
        db.close()
    except Exception as e:
        logger.error(f"Health check fallido - Error de BD: {e}", exc_info=True)
        db_status = "error"
        # Devolver 503 si la BD no está disponible
        # raise HTTPException(status_code=503, detail="Database connection error")

    return {"status": "ok", "service": "balance_service", "database": db_status}


# --- Endpoints para Cuentas Individuales (BDI) ---

@app.post("/accounts", response_model=schemas.AccountResponse, status_code=status.HTTP_201_CREATED, tags=["BDI Accounts"])
def create_account(account_in: schemas.AccountCreate, db: Session = Depends(get_db)):
    """Crea una nueva cuenta de saldo individual (BDI). Llamado por auth_service."""
    logger.info(f"Solicitud para crear cuenta individual para user_id: {account_in.user_id}")
    new_account = Account(user_id=account_in.user_id, balance=0.0)

    try:
        db.add(new_account)
        db.commit()
        db.refresh(new_account)
        logger.info(f"Cuenta individual creada exitosamente para user_id: {new_account.user_id}")
        return new_account
    except IntegrityError:
        db.rollback()
        logger.warning(f"Conflicto: Cuenta individual para user_id {account_in.user_id} ya existe.")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Account for user_id {account_in.user_id} already exists.",
        )
    except Exception as e:
         db.rollback()
         logger.error(f"Error al crear cuenta individual para user_id {account_in.user_id}: {e}", exc_info=True)
         raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Internal server error creating account.")


@app.get("/balance/{user_id}", response_model=schemas.AccountResponse, tags=["BDI Balance"])
def get_balance(user_id: int, db: Session = Depends(get_db)):
    """Obtiene los detalles y saldo de una cuenta individual (BDI)."""
    logger.debug(f"Solicitud de saldo para user_id: {user_id}")
    account = db.query(Account).filter(Account.user_id == user_id).first()
    if not account:
        logger.warning(f"Cuenta no encontrada para user_id: {user_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account for user_id {user_id} not found.",
        )
    return account

@app.post("/balance/check", status_code=status.HTTP_200_OK, tags=["BDI Balance"])
def check_funds(check_in: schemas.BalanceCheck, db: Session = Depends(get_db)):
    """Verifica si una cuenta individual (BDI) tiene fondos suficientes (sin bloqueo)."""
    logger.debug(f"Verificando fondos {check_in.amount} para user_id: {check_in.user_id}")
    account = db.query(Account).filter(Account.user_id == check_in.user_id).first()
    if not account:
        logger.warning(f"Check funds fallido: Cuenta no encontrada para user_id: {check_in.user_id}")
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Account for user_id {check_in.user_id} not found.")

    if account.balance < check_in.amount:
        logger.warning(f"Check funds fallido: Fondos insuficientes para user_id: {check_in.user_id} (Saldo: {account.balance}, Solicitado: {check_in.amount})")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Insufficient funds.")

    return {"message": "Sufficient funds."}

@app.post("/balance/credit", response_model=schemas.AccountResponse, tags=["BDI Balance"])
def credit_balance(update_in: schemas.BalanceUpdate, db: Session = Depends(get_db)):
    """Acredita (suma) fondos a una cuenta individual (BDI) usando bloqueo pesimista."""
    logger.info(f"Intentando acreditar {update_in.amount} a user_id: {update_in.user_id}")
    try:
        db.begin()
        # Bloquea la fila para la actualización
        account = db.query(Account).filter(Account.user_id == update_in.user_id).with_for_update().first()
        if not account:
            logger.warning(f"Crédito fallido: Cuenta no encontrada para user_id: {update_in.user_id}")
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Account for user_id {update_in.user_id} not found.")

        account.balance += update_in.amount
        db.commit()
        db.refresh(account)
        logger.info(f"Crédito exitoso. Nuevo balance para user_id {update_in.user_id}: {account.balance}")
        return account

    except HTTPException as http_exc:
        db.rollback()
        raise http_exc
    except Exception as e:
        db.rollback()
        logger.error(f"Error al acreditar balance para user_id {update_in.user_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Internal server error during credit.")

@app.post("/balance/debit", response_model=schemas.AccountResponse, tags=["BDI Balance"])
def debit_balance(update_in: schemas.BalanceUpdate, db: Session = Depends(get_db)):
    """Debita (resta) fondos de una cuenta individual (BDI) usando bloqueo pesimista."""
    logger.info(f"Intentando debitar {update_in.amount} de user_id: {update_in.user_id}")
    try:
        db.begin()
        # Bloquea la fila para la actualización
        account = db.query(Account).filter(Account.user_id == update_in.user_id).with_for_update().first()
        if not account:
            logger.warning(f"Débito fallido: Cuenta no encontrada para user_id: {update_in.user_id}")
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Account for user_id {update_in.user_id} not found.")

        # Doble verificación de fondos DENTRO de la transacción bloqueada
        if account.balance < update_in.amount:
            logger.warning(f"Débito fallido: Fondos insuficientes para user_id: {update_in.user_id} (Saldo: {account.balance}, Solicitado: {update_in.amount})")
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Insufficient funds at the time of debit.")

        account.balance -= update_in.amount
        db.commit()
        db.refresh(account)
        logger.info(f"Débito exitoso. Nuevo balance para user_id {update_in.user_id}: {account.balance}")
        return account

    except HTTPException as http_exc:
        db.rollback()
        raise http_exc
    except Exception as e:
        db.rollback()
        logger.error(f"Error al debitar balance para user_id {update_in.user_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Internal server error during debit.")


# --- Endpoints para Cuentas Grupales (BDG) ---

@app.post("/group_accounts", response_model=schemas.GroupAccountResponse, status_code=status.HTTP_201_CREATED, tags=["BDG Accounts"])
def create_group_account(account_in: schemas.GroupAccountCreate, db: Session = Depends(get_db)):
    """Crea una nueva cuenta de saldo para un grupo (BDG). Llamado por group_service."""
    logger.info(f"Solicitud para crear cuenta grupal para group_id: {account_in.group_id}")
    new_group_account = GroupAccount(group_id=account_in.group_id, balance=0.0)

    try:
        db.add(new_group_account)
        db.commit()
        db.refresh(new_group_account)
        logger.info(f"Cuenta grupal creada exitosamente para group_id: {new_group_account.group_id}")
        return new_group_account
    except IntegrityError:
        db.rollback()
        logger.warning(f"Conflicto: Cuenta grupal para group_id {account_in.group_id} ya existe.")
        raise HTTPException(status.HTTP_409_CONFLICT, f"Account for group_id {account_in.group_id} already exists.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error al crear cuenta grupal para group_id {account_in.group_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Internal server error creating group account.")

@app.get("/group_balance/{group_id}", response_model=schemas.GroupAccountResponse, tags=["BDG Balance"])
def get_group_balance(group_id: int, db: Session = Depends(get_db)):
    """Obtiene los detalles y saldo de una cuenta grupal (BDG)."""
    logger.debug(f"Solicitud de saldo para group_id: {group_id}")
    account = db.query(GroupAccount).filter(GroupAccount.group_id == group_id).first()
    if not account:
        logger.warning(f"Cuenta grupal no encontrada para group_id: {group_id}")
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Group account for group_id {group_id} not found.")
    return account

@app.post("/group_balance/credit", response_model=schemas.GroupAccountResponse, tags=["BDG Balance"])
def credit_group_balance(update_in: schemas.GroupBalanceUpdate, db: Session = Depends(get_db)):
    """Acredita (suma) fondos a una cuenta grupal (BDG) usando bloqueo pesimista."""
    logger.info(f"Intentando acreditar {update_in.amount} a group_id: {update_in.group_id}")
    try:
        db.begin()
        account = db.query(GroupAccount).filter(GroupAccount.group_id == update_in.group_id).with_for_update().first()
        if not account:
            logger.warning(f"Crédito grupal fallido: Cuenta no encontrada para group_id: {update_in.group_id}")
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Group account {update_in.group_id} not found.")

        account.balance += update_in.amount
        db.commit()
        db.refresh(account)
        logger.info(f"Crédito grupal exitoso. Nuevo balance para group_id {update_in.group_id}: {account.balance}")
        return account
    except HTTPException as http_exc:
        db.rollback()
        raise http_exc
    except Exception as e:
        db.rollback()
        logger.error(f"Error al acreditar balance grupal para group_id {update_in.group_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Internal server error during group credit.")

@app.post("/group_balance/debit", response_model=schemas.GroupAccountResponse, tags=["BDG Balance"])
def debit_group_balance(update_in: schemas.GroupBalanceUpdate, db: Session = Depends(get_db)):
    """Debita (resta) fondos de una cuenta grupal (BDG) usando bloqueo pesimista."""
    logger.info(f"Intentando debitar {update_in.amount} de group_id: {update_in.group_id}")
    try:
        db.begin()
        account = db.query(GroupAccount).filter(GroupAccount.group_id == update_in.group_id).with_for_update().first()
        if not account:
            logger.warning(f"Débito grupal fallido: Cuenta no encontrada para group_id: {update_in.group_id}")
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Group account {update_in.group_id} not found.")

        # Doble verificación de fondos DENTRO de la transacción bloqueada
        if account.balance < update_in.amount:
            logger.warning(f"Débito grupal fallido: Fondos insuficientes para group_id: {update_in.group_id} (Saldo: {account.balance}, Solicitado: {update_in.amount})")
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Insufficient funds in group account.")

        account.balance -= update_in.amount
        db.commit()
        db.refresh(account)
        logger.info(f"Débito grupal exitoso. Nuevo balance para group_id {update_in.group_id}: {account.balance}")
        return account
    except HTTPException as http_exc:
        db.rollback()
        raise http_exc
    except Exception as e:
        db.rollback()
        logger.error(f"Error al debitar balance grupal para group_id {update_in.group_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Internal server error during group debit.")