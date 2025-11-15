import logging
import time
import httpx
from datetime import datetime, timedelta, timezone # <-- NUEVO
from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from fastapi.responses import Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from sqlalchemy.orm import Session

# Importaciones locales
from db import engine, Base, get_db
from models import User
import db
import schemas
import models
from utils import (
    get_password_hash,
    verify_password,
    create_access_token,
    decode_token,
    BALANCE_SERVICE_URL,
    # --- NUEVAS IMPORTACIONES ---
    generate_verification_code,
    send_telegram_message,
    VERIFICATION_CODE_EXPIRATION_MINUTES
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

# Configura logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Crea tablas si no existen al iniciar
try:
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables verified/created.")
except Exception as e:
    logger.error(f"Error initializing database: {e}", exc_info=True)
    

# Inicializa FastAPI
app = FastAPI(
    title="Auth Service - Pixel Money",
    description="Handles user registration, authentication, and token verification.",
    version="1.0.0"
)

# --- Métricas Prometheus ---
REQUEST_COUNT = Counter(
    "auth_requests_total",
    "Total requests processed by Auth Service",
    ["method", "endpoint", "status_code"]
)
REQUEST_LATENCY = Histogram(
    "auth_request_latency_seconds",
    "Request latency in seconds for Auth Service",
    ["endpoint"]
)

# --- Middleware para Métricas ---
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start_time = time.time()
    response = None
    status_code = 500 # Default a 500

    try:
        response = await call_next(request)
        status_code = response.status_code
    except HTTPException as http_exc:
        status_code = http_exc.status_code
        raise http_exc
    except Exception as exc:
        logger.error(f"Unhandled exception during request processing: {exc}", exc_info=True)
        
        return Response("Internal Server Error", status_code=500)
    finally:
        latency = time.time() - start_time
        endpoint = request.url.path


        # Obtener status_code final de forma segura
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
    """Exposes application metrics for Prometheus."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health", tags=["Monitoring"])
def health_check():
    """Performs a basic health check of the service."""
    
    return {"status": "ok", "service": "auth_service"}

# --- Endpoints de API ---

@app.post("/register", response_model=schemas.UserResponse, status_code=status.HTTP_201_CREATED, tags=["Authentication"])
async def register(user: schemas.UserCreate, db: Session = Depends(get_db)):
    """
    Registers a new user with email and password.
    Creates the user in the database and calls the Balance Service to create an associated account.
    """
    logger.info(f"Registration attempt for email: {user.email}")
    db_user = db.query(User).filter(User.email == user.email).first()
    if db_user:
        logger.warning(f"Registration failed: Email {user.email} already exists.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )
    
        
    db_phone = db.query(User).filter(User.phone_number == user.phone_number).first()
    if db_phone:
        raise HTTPException(status_code=400, detail="Número de celular ya registrado")
    
    # --- NUEVO: Verificar Telegram ID ---
    db_telegram = db.query(User).filter(User.telegram_chat_id == user.telegram_chat_id).first()
    if db_telegram:
        raise HTTPException(status_code=400, detail="ID de Chat de Telegram ya registrado")

    #hashed_password = get_password_hash(user.password)
    # --- NUEVO: Generar Código y Expiración ---
    hashed_password = get_password_hash(user.password)
    verification_code = generate_verification_code()
    expires_at = datetime.utcnow() + timedelta(minutes=VERIFICATION_CODE_EXPIRATION_MINUTES)

    new_user = User(
        name=user.name,        
        email=user.email,
        hashed_password=hashed_password,
        phone_number=user.phone_number,
        telegram_chat_id=user.telegram_chat_id, # <-- NUEVO
        is_phone_verified=False, # <-- NUEVO: Inicia como no verificado
        phone_verification_code=verification_code, # <-- NUEVO
        phone_verification_expires=expires_at # <-- NUEVO
    )

    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        logger.info(f"User created with ID: {new_user.id} for email: {user.email}")
    except Exception as e:
        db.rollback()
        logger.error(f"Database error during user creation for email {user.email}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not save user.")

# --- NUEVO: Enviar código por Telegram ---
    try:
        message = f"Hola *{new_user.name}*, bienvenido a Pixel Money.\nTu código de verificación es: `{verification_code}`\nEste código expira en {VERIFICATION_CODE_EXPIRATION_MINUTES} minutos."
        send_success = await send_telegram_message(new_user.telegram_chat_id, message)
        
        if not send_success:
            # El usuario se creó, pero el envío falló.
            # El usuario puede usar "reenviar código".
            logger.warning(f"Usuario {new_user.id} creado, pero el envío inicial de Telegram falló.")
            # Opcionalmente, podrías lanzar un error aquí para informar al frontend:
            # raise HTTPException(status_code=503, detail="Usuario creado, pero falló el envío del código de verificación. Por favor, intente 'reenviar código'.")

    except Exception as e:
        logger.error(f"Excepción inesperada al enviar Telegram a {new_user.id}: {e}")
        # No revertimos la creación del usuario.

    return new_user


@app.post("/login", response_model=schemas.Token, tags=["Authentication"])
def login(db: Session = Depends(get_db), form_data: OAuth2PasswordRequestForm = Depends()):
    """
    Authenticates a user based on email (as username) and password (form-data).
    Returns a JWT access token upon successful authentication.
    """
    logger.info(f"Login attempt for user: {form_data.username}")
    user = db.query(User).filter(User.email == form_data.username).first()

    if not user or not verify_password(form_data.password, user.hashed_password):
        logger.warning(f"Login failed for user: {form_data.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

  
        # Preparamos el payload del token (¡EL ESTÁNDAR!)
    # El 'sub' (subject) es el ID del usuario.
    token_data = {
        "sub": str(user.id),
        "name": user.name # Añadimos el nombre para el frontend
    }

    access_token = create_access_token(data=token_data)
    logger.info(f"Login successful for user_id: {user.id}")

    # ¡Devolvemos el objeto COMPLETO que el schema espera!
    return {
        "access_token": access_token, 
        "token_type": "bearer",
        "user_id": user.id,
        "name": user.name,
        "email": user.email,
        "is_phone_verified": user.is_phone_verified # <-- NUEVO
    }

# --- NUEVOS ENDPOINTS DE VERIFICACIÓN ---

# En main.py

@app.post("/verify-phone", response_model=schemas.UserResponse, tags=["Authentication"])
async def verify_phone(
    verification_data: schemas.PhoneVerificationRequest, 
    db: Session = Depends(get_db)
):
    """
    Verifica un código de 6 dígitos enviado por Telegram.
    Si es exitoso, marca el teléfono como verificado y crea la cuenta de balance.
    """
    logger.info(f"Intento de verificación para {verification_data.phone_number}")
    
    user = db.query(User).filter(User.phone_number == verification_data.phone_number).first()
    
    if not user:
        logger.warning(f"Verificación fallida: Teléfono {verification_data.phone_number} no encontrado.")
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    if user.is_phone_verified:
        logger.info(f"Verificación omitida: Teléfono {verification_data.phone_number} ya está verificado.")
        raise HTTPException(status_code=400, detail="El teléfono ya está verificado")

    if user.phone_verification_code != verification_data.code:
        logger.warning(f"Verificación fallida: Código incorrecto para {verification_data.phone_number}")
        raise HTTPException(status_code=400, detail="Código de verificación incorrecto")

    
    # --- NUEVA VALIDACIÓN 1: Asegurarse de que la fecha de expiración exista ---
    if not user.phone_verification_expires:
         logger.warning(f"Verificación fallida: No hay fecha de expiración para {verification_data.phone_number}")
         # Esto podría pasar si el registro falló a la mitad
         raise HTTPException(status_code=400, detail="Código inválido o dañado. Solicite uno nuevo.")


    # Verificar expiración (ahora seguro)
    if user.phone_verification_expires < datetime.utcnow():
        logger.warning(f"Verificación fallida: Código expirado para {verification_data.phone_number}")
        raise HTTPException(status_code=400, detail="Código de verificación expirado. Solicite uno nuevo.")

    logger.info(f"Código verificado para {user.email} (ID: {user.id}). Procediendo a crear cuenta de balance.")

    
    # --- NUEVA VALIDACIÓN 2: Asegurarse de que la URL del servicio de balance exista ---
    if not BALANCE_SERVICE_URL:
        logger.critical("BALANCE_SERVICE_URL no está configurada en .env. No se puede crear la cuenta de balance.")
        # Usamos 503 (Servicio No Disponible) en lugar de 500
        raise HTTPException(status_code=503, detail="Error de configuración interna. El servicio no puede contactar al sistema de balances.")


    # --- ¡Éxito! Ahora creamos la cuenta de balance ---
    async with httpx.AsyncClient() as client:
        try:
            create_account_url = f"{BALANCE_SERVICE_URL}/accounts"
            response = await client.post(create_account_url, json={"user_id": user.id})
            response.raise_for_status() 
            logger.info(f"Llamada exitosa a Balance Service para user_id: {user.id}")
        
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.error(f"Fallo al llamar a Balance Service para user_id {user.id} DURANTE LA VERIFICACIÓN: {exc}", exc_info=True)
            
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            detail = "Servicio de Balance no disponible. Intente verificar nuevamente en unos minutos."
            
            if isinstance(exc, httpx.HTTPStatusError):
                status_code = exc.response.status_code
                try:
                    detail = f"Error de Balance Service: {exc.response.json().get('detail', exc.response.text)}"
                except:
                    detail = f"Error de Balance Service: Status {status_code}"

            raise HTTPException(status_code=status_code, detail=detail)

    # --- Actualizar usuario en la BD ---
    try:
        user.is_phone_verified = True
        user.phone_verification_code = None # Invalidar código
        user.phone_verification_expires = None
        db.commit()
        db.refresh(user)
        logger.info(f"Usuario {user.id} marcado como verificado.")
    except Exception as e:
        db.rollback()
        logger.critical(f"CRÍTICO: No se pudo actualizar el estado verificado del usuario {user.id} después de crear la cuenta de balance: {e}")
        raise HTTPException(status_code=500, detail="Error al finalizar la verificación del usuario. Contacte a soporte.")

    return user

@app.post("/resend-code", status_code=status.HTTP_204_NO_CONTENT, tags=["Authentication"])
async def resend_verification_code(
    request_data: schemas.RequestVerificationCode,
    db: Session = Depends(get_db)
):
    """
    Genera un nuevo código de verificación y lo reenvía por Telegram.
    """
    logger.info(f"Solicitud de reenvío de código para {request_data.phone_number}")
    user = db.query(User).filter(User.phone_number == request_data.phone_number).first()

    if not user:
        logger.warning(f"Reenvío fallido: Teléfono {request_data.phone_number} no encontrado.")
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    if user.is_phone_verified:
        logger.warning(f"Reenvío fallido: Teléfono {request_data.phone_number} ya verificado.")
        raise HTTPException(status_code=400, detail="El teléfono ya está verificado")

    # Generar nuevo código y expiración
    verification_code = generate_verification_code()
    expires_at = datetime.utcnow() + timedelta(minutes=VERIFICATION_CODE_EXPIRATION_MINUTES)

    try:
        user.phone_verification_code = verification_code
        user.phone_verification_expires = expires_at
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error de BD al actualizar el código de reenvío para {user.id}: {e}")
        raise HTTPException(status_code=500, detail="Error al guardar el nuevo código.")
    
    # Enviar por Telegram
    try:
        message = f"Hola *{user.name}*,\nTu *nuevo* código de verificación para Pixel Money es: `{verification_code}`"
        send_success = await send_telegram_message(user.telegram_chat_id, message)
        
        if not send_success:
                logger.error(f"Fallo el reenvío de Telegram para {user.id}")
                raise HTTPException(status_code=503, detail="Error al enviar el código de verificación. Inténtalo de nuevo.")

    except Exception as e:
        logger.error(f"Excepción inesperada al reenviar Telegram a {user.id}: {e}")
        raise HTTPException(status_code=500, detail="Error interno del servicio de notificación.")
    
    # Retorna 204 No Content si todo fue bien
    return Response(status_code=status.HTTP_204_NO_CONTENT)

@app.get("/users/{user_id}", response_model=schemas.UserResponse, tags=["Users"])
async def get_user_by_id(user_id: int, db: Session = Depends(get_db)):
    """
    Retorna la información del usuario por su ID.
    Usado internamente por el API Gateway al llamar /auth/me.
    """
    logger.info(f"Solicitud de datos para usuario con ID {user_id}")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        logger.warning(f"Usuario con ID {user_id} no encontrado.")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado.",
        )

    logger.info(f"Usuario encontrado: {user.email} (ID: {user.id})")

    return user


@app.get("/verify", response_model=schemas.TokenPayload, tags=["Internal"])
def verify(token: str): 
    """
    Valida un token JWT (pasado como query parameter 'token') y devuelve su payload.
    Usado por el API Gateway.
    """
    payload = decode_token(token)
    if payload is None or "sub" not in payload: # Doble verificación
     logger.warning("Intento de verificación con token inválido, expirado o sin 'sub'.")
     raise HTTPException(
         status_code=status.HTTP_401_UNAUTHORIZED,
         detail="Invalid or expired token",
     )

    # Devolvemos el payload que SÍ coincide con schemas.TokenPayload
    # (El gateway_service ahora leerá 'sub' sin problemas)
    return {"sub": payload.get("sub"), "exp": payload.get("exp"), "name": payload.get("name")}



@app.get("/users/by-phone/{phone_number}", response_model=schemas.UserResponse, tags=["Users"])
def get_user_by_phone(phone_number: str, db: Session = Depends(get_db)):
    """
    Busca un usuario por su número de celular.
    (Usado internamente por ledger_service para transferencias P2P).
    """
    logger.info(f"Buscando usuario por número de celular: {phone_number}")
    db_user = db.query(User).filter(User.phone_number == phone_number).first()
    if db_user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado con ese número de celular")
    return db_user
