# balance_service/main.py
from fastapi import FastAPI, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
import logging # Para registrar errores

# Importaciones locales
from .db import engine, Base, get_db
from .models import Account
from . import schemas

# Configura un logger básico
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Crea las tablas en la base de datos (solo la tabla `accounts`)
Base.metadata.create_all(bind=engine)

# Inicializa la aplicación FastAPI
app = FastAPI(title="Balance Service - Bank A")

# --- Endpoints de la API ---

@app.post("/accounts", response_model=schemas.Account, status_code=status.HTTP_201_CREATED)
def create_account(account_in: schemas.AccountCreate, db: Session = Depends(get_db)):
    """
    Crea una nueva cuenta de saldo para un usuario.
    Llamado por auth_service justo después del registro.
    """
    new_account = Account(user_id=account_in.user_id, balance=0.0)
    
    try:
        db.add(new_account)
        db.commit()
        db.refresh(new_account)
        logger.info(f"Cuenta creada para user_id: {new_account.user_id}")
        return new_account
    except IntegrityError:
        # Esto pasa si el user_id ya tiene una cuenta (Error de Clave Única)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"La cuenta para el user_id {account_in.user_id} ya existe.",
        )

@app.get("/balance/{user_id}", response_model=schemas.Account)
def get_balance(user_id: int, db: Session = Depends(get_db)):
    """
    Obtiene el saldo actual de un usuario.
    """
    account = db.query(Account).filter(Account.user_id == user_id).first()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cuenta para user_id {user_id} no encontrada.",
        )
    return account

@app.post("/balance/check", status_code=status.HTTP_200_OK)
def check_funds(check_in: schemas.BalanceCheck, db: Session = Depends(get_db)):
    """
    Verifica si un usuario tiene fondos suficientes para una transacción.
    Llamado por ledger_service ANTES de una transferencia.
    """
    account = db.query(Account).filter(Account.user_id == check_in.user_id).first()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cuenta para user_id {check_in.user_id} no encontrada.",
        )
    
    if account.balance < check_in.amount:
        logger.warning(f"Fondos insuficientes para user_id: {check_in.user_id}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Fondos insuficientes."
        )
        
    return {"message": "Fondos suficientes."}

@app.post("/balance/credit", response_model=schemas.Account)
def credit_balance(update_in: schemas.BalanceUpdate, db: Session = Depends(get_db)):
    """
    Acredita (suma) dinero a una cuenta.
    Llamado por ledger_service DESPUÉS de un depósito exitoso.
    """
    account = db.query(Account).filter(Account.user_id == update_in.user_id).first()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cuenta para user_id {update_in.user_id} no encontrada.",
        )
    
    # Aquí podríamos añadir bloqueo de fila (FOR UPDATE) en producción
    account.balance += update_in.amount
    db.commit()
    db.refresh(account)
    logger.info(f"Crédito de {update_in.amount} aplicado a user_id: {update_in.user_id}")
    return account

@app.post("/balance/debit", response_model=schemas.Account)
def debit_balance(update_in: schemas.BalanceUpdate, db: Session = Depends(get_db)):
    """
    Debita (resta) dinero de una cuenta.
    Llamado por ledger_service DESPUÉS de una transferencia externa exitosa.
    """
    account = db.query(Account).filter(Account.user_id == update_in.user_id).first()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cuenta para user_id {update_in.user_id} no encontrada.",
        )
        
    if account.balance < update_in.amount:
        logger.error(f"¡SOBREGIRO! user_id: {update_in.user_id} intentó debitar {update_in.amount} pero solo tiene {account.balance}")
        # Esto NO debería pasar si el ledger_service llamó a /check primero
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Fondos insuficientes. El débito no debería haber sido autorizado."
        )

    account.balance -= update_in.amount
    db.commit()
    db.refresh(account)
    logger.info(f"Débito de {update_in.amount} aplicado a user_id: {update_in.user_id}")
    return account