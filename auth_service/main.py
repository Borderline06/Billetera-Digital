# auth_service/main.py
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
import httpx # Para llamar al balance_service

# Importaciones locales
from .db import engine, Base, get_db
from .models import User
from . import schemas # Crearemos este archivo para los modelos Pydantic
from .utils import (
    get_password_hash, 
    verify_password, 
    create_access_token, 
    decode_token,
    BALANCE_SERVICE_URL
)

# Crea las tablas en la base de datos si no existen
# (SQLAlchemy se encarga de esto basado en nuestros modelos)
Base.metadata.create_all(bind=engine)

# Inicializa la aplicación FastAPI
app = FastAPI(title="Auth Service - Bank A")

# --- Endpoints de la API ---

@app.post("/register", response_model=schemas.UserResponse, status_code=status.HTTP_201_CREATED)
async def register(user: schemas.UserCreate, db: Session = Depends(get_db)):
    """
    Registra un nuevo usuario.
    Verifica si el email ya existe, hashea la contraseña, crea el usuario
    y llama al balance_service para crear la cuenta asociada.
    """
    db_user = db.query(User).filter(User.email == user.email).first()
    if db_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )
    
    hashed_password = get_password_hash(user.password)
    new_user = User(email=user.email, hashed_password=hashed_password)
    
    db.add(new_user)
    db.commit()
    db.refresh(new_user) # Obtenemos el ID asignado por la BD

    # --- Llamada al Balance Service para crear la cuenta ---
    # Usamos httpx para hacer una llamada asíncrona interna
    async with httpx.AsyncClient() as client:
        try:
            # Asume que balance_service tiene un endpoint /accounts
            create_account_url = f"{BALANCE_SERVICE_URL}/accounts" 
            response = await client.post(create_account_url, json={"user_id": new_user.id})
            response.raise_for_status() # Lanza excepción si balance_service falla
        except httpx.RequestError as exc:
            # Si no se puede crear la cuenta, revertimos la creación del usuario
            # (Esto es una simplificación, idealmente usaríamos Sagas o transacciones distribuidas)
            db.delete(new_user)
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Failed to create account at Balance Service: {exc}",
            )
        except httpx.HTTPStatusError as exc:
             db.delete(new_user)
             db.commit()
             raise HTTPException(
                status_code=exc.response.status_code,
                detail=f"Balance Service returned an error: {exc.response.text}",
            )

    # Devolvemos solo el email y el ID (sin la contraseña)
    return {"id": new_user.id, "email": new_user.email}


@app.post("/login", response_model=schemas.Token)
def login(db: Session = Depends(get_db), form_data: OAuth2PasswordRequestForm = Depends()):
    """
    Autentica al usuario usando email y contraseña (form-data).
    Devuelve un token JWT si las credenciales son válidas.
    """
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Creamos el token con el email del usuario como identificador ("subject")
    access_token = create_access_token(data={"sub": str(user.id)})
    
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/verify", response_model=schemas.TokenPayload)
def verify(token: str):
    """
    Verifica la validez de un token JWT.
    Usado por el API Gateway para validar las peticiones.
    """
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    # Devolvemos el contenido del token (payload) si es válido
    # El payload contiene el "sub" (subject, que es el email) y "exp" (expiration)
    return payload