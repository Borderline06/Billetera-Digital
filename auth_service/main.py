import logging
import time
import httpx
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
    

    hashed_password = get_password_hash(user.password)

    new_user = User(
        name=user.name,        
        email=user.email,
        hashed_password=hashed_password,
        phone_number=user.phone_number
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

    
    async with httpx.AsyncClient() as client:
        try:
            create_account_url = f"{BALANCE_SERVICE_URL}/accounts"
            response = await client.post(create_account_url, json={"user_id": new_user.id})
            response.raise_for_status() 
            logger.info(f"Successfully called Balance Service for user_id: {new_user.id}")
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.error(f"Failed to call Balance Service for user_id {new_user.id}: {exc}", exc_info=True)
            
            logger.warning(f"Attempting to revert user creation for user_id {new_user.id} due to Balance Service failure.")
            try:
                db.delete(new_user)
                db.commit()
                logger.info(f"Successfully reverted user creation for user_id {new_user.id}.")
            except Exception as delete_e:
                
                logger.critical(f"CRITICAL: Failed to revert user creation for user_id {new_user.id}: {delete_e}", exc_info=True)
               

            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            detail = f"Balance Service unavailable or failed."
            if isinstance(exc, httpx.HTTPStatusError):
                status_code = exc.response.status_code
                try: 
                    detail = f"Balance Service error: {exc.response.json().get('detail', exc.response.text)}"
                except: 
                     detail = f"Balance Service error: Status {status_code}"
            raise HTTPException(status_code=status_code, detail=detail)

    return {
        "id": new_user.id,
        "name": new_user.name,
        "email": new_user.email,
        "phone_number": new_user.phone_number
    }


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

  
    access_token = create_access_token(data={"sub": str(user.id)})
    logger.info(f"Login successful for user_id: {user.id}")
    return {"access_token": access_token, "token_type": "bearer"}

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

    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "phone_number": user.phone_number
    }


@app.get("/verify", response_model=schemas.TokenPayload, tags=["Internal"])
def verify(token: str): 
    """
    Valida un token JWT (pasado como query parameter 'token') y devuelve su payload.
    Usado por el API Gateway.
    """
    payload = decode_token(token)
    if payload is None:
        logger.warning("Intento de verificación con token inválido o expirado.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    
    return payload



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

