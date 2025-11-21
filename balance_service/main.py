import logging
import time
import os
import httpx # <--- ¡Vital para llamar a RENIEC y al Ledger!
from decimal import Decimal
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, status, Request, Header
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from fastapi.responses import Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from dotenv import load_dotenv

# Importaciones locales
from db import engine, Base, get_db, SessionLocal
from models import Account, GroupAccount, Loan, LoanStatus
import schemas

# Carga variables de entorno
load_dotenv()

# Configura logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# URLs y Claves
LEDGER_SERVICE_URL = os.getenv("LEDGER_SERVICE_URL")
DECOLECTA_API_URL = os.getenv("DECOLECTA_API_URL")
DECOLECTA_TOKEN = os.getenv("DECOLECTA_TOKEN")

# Inicializa BD
try:
    Base.metadata.create_all(bind=engine)
    logger.info("Tablas de base de datos verificadas/creadas.")
except Exception as e:
    logger.error(f"Error al inicializar la base de datos: {e}", exc_info=True)

app = FastAPI(
    title="Balance Service - Pixel Money",
    description="Gestiona saldos, préstamos con interés y validación RENIEC.",
    version="2.0.0"
)

# --- Métricas Prometheus (Resumido para ahorrar espacio) ---
REQUEST_COUNT = Counter("balance_requests_total", "Total requests", ["method", "endpoint", "status_code"])
REQUEST_LATENCY = Histogram("balance_request_latency_seconds", "Request latency", ["endpoint"])

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
        REQUEST_LATENCY.labels(endpoint=endpoint).observe(latency)
        final_code = getattr(response, 'status_code', status_code)
        REQUEST_COUNT.labels(method=request.method, endpoint=endpoint, status_code=final_code).inc()
    return response

@app.get("/metrics", tags=["Monitoring"])
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health", tags=["Monitoring"])
def health_check():
    return {"status": "ok", "service": "balance_service"}

# --- HELPER: Validación DNI (RENIEC REAL) ---
async def validar_dni_reniec(dni: str) -> str:
    """
    Consulta la API de Decolecta para validar si el DNI es real.
    Retorna el Nombre Completo si existe.
    """
    if not dni or len(dni) != 8 or not dni.isdigit():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "DNI inválido. Debe tener 8 dígitos numéricos.")

    if not DECOLECTA_API_URL or not DECOLECTA_TOKEN:
        logger.warning("⚠️ API RENIEC no configurada en .env. Saltando validación real.")
        return "Usuario Validado (Modo Dev)"

    try:
        async with httpx.AsyncClient() as client:
            # GET https://api.decolecta.com/v1/reniec/dni?numero=XXXXXXXX
            logger.info(f"Consultando RENIEC para DNI: {dni}")
            response = await client.get(
                f"{DECOLECTA_API_URL}?numero={dni}",
                headers={"Authorization": f"Bearer {DECOLECTA_TOKEN}"},
                timeout=5.0
            )
            
            if response.status_code == 200:
                data = response.json()
                # La API devuelve: { "full_name": "NOMBRE...", ... }
                nombre = data.get("full_name") or f"{data.get('nombres')} {data.get('apellido_paterno')}"
                logger.info(f"✅ DNI Validado: {nombre}")
                return nombre
            
            elif response.status_code == 404 or response.status_code == 422:
                logger.warning(f"❌ DNI {dni} no encontrado en RENIEC.")
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "El DNI ingresado no existe en los registros de RENIEC.")
            else:
                logger.error(f"Error API RENIEC: {response.status_code}")
                # En caso de error del servicio externo, permitimos continuar con warning (Fail Open) o bloqueamos (Fail Closed).
                # Para tu proyecto, mejor dejar pasar para no bloquear la demo si la API falla.
                return "Validación Pendiente (Error API)"

    except httpx.RequestError as e:
        logger.error(f"Error de conexión con RENIEC: {e}")
        return "Validación Pendiente (Timeout)"


# balance_service/main.py - PARTE 2 (Pegar debajo de la Parte 1)

# --- Endpoints: Cuentas Individuales (BDI) ---

@app.post("/accounts", response_model=schemas.AccountResponse, status_code=status.HTTP_201_CREATED, tags=["BDI Accounts"])
def create_account(account_in: schemas.AccountCreate, db: Session = Depends(get_db)):
    new_account = Account(user_id=account_in.user_id, balance=0.0)
    try:
        db.add(new_account)
        db.commit()
        db.refresh(new_account)
        return new_account
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="La cuenta ya existe.")

@app.get("/balance/{user_id}", response_model=schemas.AccountResponse, tags=["BDI Balance"])
def get_balance(user_id: int, db: Session = Depends(get_db)):
    # Usamos joinedload para traer el préstamo activo si existe
    account = db.query(Account).options(joinedload(Account.loan)).filter(Account.user_id == user_id).first()
    if not account:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Cuenta no encontrada.")
    return account

@app.post("/balance/check", tags=["BDI Balance"])
def check_funds(check_in: schemas.BalanceCheck, db: Session = Depends(get_db)):
    amount_check = Decimal(str(check_in.amount))
    account = db.query(Account).filter(Account.user_id == check_in.user_id).first()
    if not account:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Cuenta no encontrada.")
    if account.balance < amount_check:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Fondos insuficientes.")
    return {"message": "Sufficient funds"}

@app.post("/balance/credit", response_model=schemas.AccountResponse, tags=["BDI Balance"])
def credit_balance(update_in: schemas.BalanceUpdate, db: Session = Depends(get_db)):
    # Llamado por el Ledger
    try:
        with db.begin():
            account = db.query(Account).filter(Account.user_id == update_in.user_id).with_for_update().first()
            if not account:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Cuenta no encontrada.")
            account.balance += Decimal(str(update_in.amount))
            db.commit()
        db.refresh(account)
        return account
    except Exception as e:
        db.rollback()
        raise e

@app.post("/balance/debit", response_model=schemas.AccountResponse, tags=["BDI Balance"])
def debit_balance(update_in: schemas.BalanceUpdate, db: Session = Depends(get_db)):
    # Llamado por el Ledger
    try:
        with db.begin():
            account = db.query(Account).filter(Account.user_id == update_in.user_id).with_for_update().first()
            if not account:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Cuenta no encontrada.")
            
            amount = Decimal(str(update_in.amount))
            if account.balance < amount:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Fondos insuficientes.")
            
            account.balance -= amount
            db.commit()
        db.refresh(account)
        return account
    except Exception as e:
        db.rollback()
        raise e

# --- Endpoints: Cuentas Grupales (BDG) ---

@app.post("/group_accounts", response_model=schemas.GroupAccount, status_code=status.HTTP_201_CREATED, tags=["Balance - Grupal"])
def create_group_account(account_in: schemas.GroupAccountCreate, db: Session = Depends(get_db)):
    try:
        new_account = GroupAccount(group_id=account_in.group_id, balance=0.00)
        db.add(new_account)
        db.commit()
        db.refresh(new_account)
        return new_account
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Cuenta grupal ya existe.")

@app.get("/group_balance/{group_id}", response_model=schemas.GroupAccount, tags=["Balance - Grupal"])
def get_group_balance(group_id: int, db: Session = Depends(get_db)):
    account = db.query(GroupAccount).filter(GroupAccount.group_id == group_id).first()
    if not account:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cuenta grupal no encontrada.")
    return account

@app.post("/group_balance/credit", response_model=schemas.GroupAccount, tags=["Balance - Grupal"])
def credit_group_balance(update_in: schemas.GroupBalanceUpdate, db: Session = Depends(get_db)):
    try:
        with db.begin():
            account = db.query(GroupAccount).filter(GroupAccount.group_id == update_in.group_id).with_for_update().first()
            if not account:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Cuenta grupal no encontrada.")
            account.balance += Decimal(str(update_in.amount))
            db.commit()
        db.refresh(account)
        return account
    except Exception as e:
        db.rollback()
        raise e

@app.post("/group_balance/debit", response_model=schemas.GroupAccount, tags=["Balance - Grupal"])
def debit_group_balance(update_in: schemas.GroupBalanceUpdate, db: Session = Depends(get_db)):
    try:
        with db.begin():
            account = db.query(GroupAccount).filter(GroupAccount.group_id == update_in.group_id).with_for_update().first()
            if not account:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Cuenta grupal no encontrada.")
            
            amount = Decimal(str(update_in.amount))
            if account.balance < amount:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "El grupo no tiene fondos suficientes.")
            
            account.balance -= amount
            db.commit()
        db.refresh(account)
        return account
    except Exception as e:
        db.rollback()
        raise e
    



# --- Endpoints: Préstamos (Loans) con SAGA ---

@app.post("/request-loan", response_model=schemas.AccountResponse, tags=["BDI Préstamos"])
async def request_loan(
    req: schemas.DepositRequest,
    x_user_id: int = Header(..., alias="X-User-ID"),
    db: Session = Depends(get_db)
):
    """
    Solicita préstamo. Valida DNI en RENIEC, calcula 5% interés y llama al Ledger.
    """
    user_id = x_user_id
    amount_principal = Decimal(str(req.amount))
    
    # 1. Validación RENIEC (Anti-Fraude)
    # Si el usuario huye, tenemos su nombre real.
    nombre_real = await validar_dni_reniec(req.dni)
    logger.info(f"Solicitud de préstamo para {user_id}. DNI validado: {nombre_real}")

    if not LEDGER_SERVICE_URL:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Falta configuración del Ledger.")

    # 2. Reglas de Negocio
    MAX_LOAN = Decimal('500.00')
    INTEREST_RATE = Decimal('0.05') # 5% de interés

    if amount_principal > MAX_LOAN:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Monto excede el límite (S/ {MAX_LOAN}).")

    # Cálculo de la deuda total (Principal + Interés)
    # Si pides 100, debes 105.
    total_debt = amount_principal * (1 + INTEREST_RATE)

    try:
        # 3. Guardar Préstamo en BD (Estado ACTIVE)
        # Usamos una transacción corta solo para el préstamo
        existing_loan = db.query(Loan).filter(Loan.user_id == user_id, Loan.status == LoanStatus.ACTIVE).first()
        if existing_loan:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ya tienes un préstamo activo. Paga primero.")

        new_loan = Loan(
            user_id=user_id,
            principal_amount=amount_principal,
            outstanding_balance=total_debt, # ¡Aquí guardamos la deuda con interés!
            interest_rate=INTEREST_RATE * 100,
            status=LoanStatus.ACTIVE
        )
        db.add(new_loan)
        db.commit()
        db.refresh(new_loan)

        # 4. SAGA: Llamar al Ledger para el Desembolso
        # El Ledger llamará a nuestro endpoint /balance/credit para poner la plata.
        async with httpx.AsyncClient() as client:
            ledger_res = await client.post(
                f"{LEDGER_SERVICE_URL}/loans/disbursement",
                json={
                    "user_id": user_id,
                    "amount": float(amount_principal), # Desembolsamos solo lo que pidió (100)
                    "loan_id": new_loan.id
                }
            )
            ledger_res.raise_for_status()

        # 5. Retornar estado actual
        account = db.query(Account).filter(Account.user_id == user_id).first()
        return account

    except httpx.HTTPStatusError as e:
        logger.error(f"Fallo en Ledger al desembolsar: {e.response.text}")
        # Rollback manual: Borramos el préstamo porque no se entregó el dinero
        try:
            db.delete(new_loan)
            db.commit()
        except: pass
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Error en el sistema financiero (Ledger).")
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Error crítico en request_loan: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno al procesar el préstamo.")


@app.post("/pay-loan", response_model=schemas.LoanResponse, tags=["BDI Préstamos"])
async def pay_loan(
    x_user_id: int = Header(..., alias="X-User-ID"),
    db: Session = Depends(get_db)
):
    """
    Paga la deuda total. Llama al Ledger para descontar el saldo.
    """
    user_id = x_user_id
    
    if not LEDGER_SERVICE_URL:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Falta configuración del Ledger.")

    # 1. Buscar deuda
    loan = db.query(Loan).filter(Loan.user_id == user_id, Loan.status == LoanStatus.ACTIVE).first()
    if not loan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No tienes préstamos activos.")

    amount_to_pay = loan.outstanding_balance # Pagamos todo (105)

    try:
        # 2. SAGA: Llamar al Ledger para Cobrar
        # El Ledger llamará a /balance/debit. Si no hay saldo, fallará aquí.
        async with httpx.AsyncClient() as client:
            ledger_res = await client.post(
                f"{LEDGER_SERVICE_URL}/loans/payment",
                json={
                    "user_id": user_id,
                    "amount": float(amount_to_pay),
                    "loan_id": loan.id
                }
            )
            ledger_res.raise_for_status()

        # 3. Si el cobro pasó, cerramos el préstamo
        loan.outstanding_balance = Decimal('0.00')
        loan.status = LoanStatus.PAID
        db.commit()
        db.refresh(loan)
        
        return loan

    except httpx.HTTPStatusError as e:
        detail = "Error al procesar el pago."
        try: detail = e.response.json().get('detail', detail)
        except: pass
        raise HTTPException(status_code=e.response.status_code, detail=detail)
    except Exception as e:
        logger.error(f"Error pagando préstamo: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno al pagar.")