# gateway_service/main.py
import os
import httpx
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

# --- Cargamos las direcciones de los servicios internos ---
AUTH_URL = os.getenv("AUTH_SERVICE_URL")
BALANCE_URL = os.getenv("BALANCE_SERVICE_URL")
LEDGER_URL = os.getenv("LEDGER_SERVICE_URL")

app = FastAPI(title="API Gateway - Bank A")

# Lista de rutas que NO requieren un token JWT
PUBLIC_ROUTES = [
    "/auth/login",
    "/auth/register",
    "/health", # Importante para los healthchecks
    "/metrics" # Importante para Prometheus
]

# Creamos un cliente HTTP que puede ser reutilizado
# (Mejora el rendimiento al no abrir y cerrar conexiones)
client = httpx.AsyncClient()

# --- Middleware de Seguridad (El "Guardia") ---
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """
    Intercepta CADA petición.
    1. Revisa si la ruta es pública.
    2. Si no es pública, exige un token JWT.
    3. Valida el token llamando al auth_service (/verify).
    4. Si es válido, extrae el user_id y lo "adjunta" a la petición
       para que los endpoints de proxy lo usen.
    """
    
    # Añadimos /docs y /openapi.json a las rutas públicas para ver la documentación
    if request.url.path in PUBLIC_ROUTES or request.url.path.startswith("/docs") or request.url.path.startswith("/openapi.json"):
        return await call_next(request)

    # 1. No es pública, exigimos token
    token = request.headers.get("Authorization")
    if not token:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Missing Authorization header"},
        )

    if not token.startswith("Bearer "):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid token format"},
        )
    
    token_value = token.split(" ")[1] # Extraemos el token

    # 2. Validamos el token llamando al auth_service
    try:
        verify_url = f"{AUTH_URL}/verify?token={token_value}"
        response = await client.get(verify_url)
        
        # Si auth_service dice que el token es inválido
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.json().get("detail"))

        # 3. El token es válido. Adjuntamos el payload a la petición.
        # (Gracias a nuestra corrección, el payload contiene el user_id en 'sub')
        token_payload = response.json()
        request.state.user_id = int(token_payload.get("sub")) # Adjuntamos el user_id

    except httpx.RequestError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Authentication service is unavailable"},
        )
    except Exception as e:
         return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": f"Invalid token: {e}"},
        )
    
    # Si todo está bien, pasamos al endpoint de proxy
    return await call_next(request)


# --- Endpoints Públicos (Proxy para Auth) ---

@app.post("/auth/register")
async def proxy_register(request: Request):
    """
    Proxy para registrar un usuario.
    Toma el JSON del cliente y lo reenvía al auth_service.
    """
    payload = await request.json()
    response = await client.post(f"{AUTH_URL}/register", json=payload)
    return JSONResponse(status_code=response.status_code, content=response.json())

@app.post("/auth/login")
async def proxy_login(request: Request):
    """
    Proxy para iniciar sesión.
    Toma los form-data del cliente y los reenvía al auth_service.
    """
    form_data = await request.form()
    response = await client.post(f"{AUTH_URL}/login", data=form_data)
    return JSONResponse(status_code=response.status_code, content=response.json())


# --- Endpoints de Salud (para monitoreo) ---

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "gateway"}


# ... (todo el código anterior de gateway_service/main.py) ...

@app.get("/balance/me")
async def proxy_get_my_balance(request: Request):
    # ... (este endpoint ya lo teníamos) ...
    user_id = request.state.user_id 
    response = await client.get(f"{BALANCE_URL}/balance/{user_id}")
    return JSONResponse(status_code=response.status_code, content=response.json())

# --- INICIO DEL NUEVO CÓDIGO (SPRINT 2) ---

@app.post("/ledger/deposit")
async def proxy_deposit(request: Request):
    """
    Proxy para depósitos. Es una ruta PROTEGIDA.
    1. El middleware ya validó el token.
    2. Extraemos el user_id del token (inyectado por el middleware).
    3. Extraemos el JSON del cliente.
    4. Inyectamos el user_id en el JSON y lo reenviamos al ledger_service.
    """
    # 1. Obtenemos el user_id del token (¡seguro!)
    user_id = request.state.user_id
    
    # 2. Obtenemos la clave de idempotencia (¡seguridad!)
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
         return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Missing Idempotency-Key header"},
        )
    
    # 3. Preparamos el payload
    payload = await request.json()
    
    # 4. Inyectamos el user_id. El cliente NO envía el user_id;
    # lo inyectamos desde el token para que no puedan depositar en otra cuenta.
    payload['user_id'] = user_id
    
    headers = {"Idempotency-Key": idempotency_key}
    
    # 5. Reenviamos al ledger_service
    response = await client.post(f"{LEDGER_URL}/deposit", json=payload, headers=headers)
    return JSONResponse(status_code=response.status_code, content=response.json())


@app.post("/ledger/transfer")
async def proxy_transfer(request: Request):
    """
    Proxy para transferencias. Ruta PROTEGIDA.
    Misma lógica de seguridad que el depósito: inyectamos el user_id.
    """
    # 1. Obtenemos el user_id del token (¡seguro!)
    user_id = request.state.user_id
    
    # 2. Obtenemos la clave de idempotencia (¡seguridad!)
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
         return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Missing Idempotency-Key header"},
        )
    
    # 3. Preparamos el payload
    payload = await request.json()
    
    # 4. Inyectamos el user_id desde el token
    payload['user_id'] = user_id
    
    headers = {"Idempotency-Key": idempotency_key}
    
    # 5. Reenviamos al ledger_service
    response = await client.post(f"{LEDGER_URL}/transfer", json=payload, headers=headers)
    return JSONResponse(status_code=response.status_code, content=response.json())


# --- FIN DEL NUEVO CÓDIGO (SPRINT 2) ---

# ... (El endpoint @app.get("/health") se mantiene al final) ...
@app.get("/health")
def health_check():
    return {"status": "ok", "service": "gateway"}