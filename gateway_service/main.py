"""API Gateway para Pixel Money. Punto de entrada único, maneja autenticación y enrutamiento."""

import os
import httpx
import logging
import time
import json
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, Request, HTTPException, status, Header, Depends
from fastapi.responses import JSONResponse, Response
from dotenv import load_dotenv
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from typing import Optional 

# Carga variables de entorno
load_dotenv()

# Configura logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- URLs de Servicios Internos ---
AUTH_URL = os.getenv("AUTH_SERVICE_URL")
BALANCE_URL = os.getenv("BALANCE_SERVICE_URL")
LEDGER_URL = os.getenv("LEDGER_SERVICE_URL")
GROUP_URL = os.getenv("GROUP_SERVICE_URL")

# Verifica que las URLs estén definidas
required_urls = {"AUTH_URL", "BALANCE_URL", "LEDGER_URL", "GROUP_URL"}
missing_urls = required_urls - set(os.environ)
if missing_urls:
    logger.critical(f"Faltan URLs de servicios internos en .env: {', '.join(missing_urls)}")
    # El gateway no puede funcionar sin estas URLs.
    # Lanzamos una excepción para detener el arranque.
    raise EnvironmentError(f"Faltan URLs de servicios internos: {', '.join(missing_urls)}")

# Inicializa FastAPI
app = FastAPI(
    title="API Gateway - Pixel Money",
    description="Punto de entrada único para todos los servicios de la billetera digital.",
    version="1.0.0"
)

# --- Configuración de CORS ---
origins = [
    "http://localhost",
    "http://localhost:3001",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True, 
    allow_methods=["*"],    
    allow_headers=["*"],    
)



# --- Rutas Públicas (no requieren token) ---
PUBLIC_ROUTES = [
    "/auth/login",
    "/auth/register",
    "/health",
    "/metrics",
    "/docs",
    "/openapi.json"
]

# --- Cliente HTTP Asíncrono Reutilizable ---
client = httpx.AsyncClient(timeout=15.0)

# --- Métricas Prometheus ---
REQUEST_COUNT = Counter(
    "gateway_requests_total",
    "Total requests processed by API Gateway",
    ["method", "endpoint", "status_code"]
)
REQUEST_LATENCY = Histogram(
    "gateway_request_latency_seconds",
    "Request latency in seconds for API Gateway",
    ["endpoint"]
)

# --- Middlewares (Seguridad y Métricas) ---

@app.middleware("http")
async def combined_middleware(request: Request, call_next):
    """Middleware combinado para métricas y seguridad."""
    start_time = time.time()
    response = None
    status_code = 500
    user_id = None

    endpoint = request.url.path

    try:
        if request.method == "OPTIONS":
            response = await call_next(request)
            status_code = response.status_code 
            return response
        # --- Lógica de Seguridad (Autenticación) ---
        request.state.user_id = user_id # Inicializar
        is_public = any(request.url.path.startswith(p) for p in PUBLIC_ROUTES)

        if not is_public:
            token = request.headers.get("Authorization")
            if not token or not token.startswith("Bearer "):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Cabecera Authorization ausente o inválida")

            token_value = token.split(" ")[1]
            try:
                verify_url = f"{AUTH_URL}/verify?token={token_value}"
                verify_response = await client.get(verify_url)

                if verify_response.status_code != 200:
                    detail = verify_response.json().get("detail", "Token inválido")
                    raise HTTPException(verify_response.status_code, detail)

                token_payload = verify_response.json()
                user_id_str = token_payload.get("sub") or token_payload.get("user_id")
                if user_id_str:
                    user_id = int(user_id_str)
                    request.state.user_id = user_id # Inyectamos user_id para los endpoints
                else:
                    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Payload del token inválido (sin 'sub')")

            except httpx.RequestError:
                 raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Servicio de autenticación no disponible")
            except ValueError:
                 raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Payload del token inválido ('sub' no es un ID válido)")
            except Exception as auth_exc:
                 logger.error(f"Error inesperado en validación de token: {auth_exc}", exc_info=True)
                 raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Error en validación de token")

        # --- Llamar al siguiente middleware o endpoint ---
        response = await call_next(request)
        status_code = response.status_code

    except HTTPException as http_exc:
        # Captura errores HTTP lanzados por el middleware o el endpoint
        status_code = http_exc.status_code
        response = JSONResponse(status_code=status_code, content={"detail": http_exc.detail})
    
    except Exception as exc:
        # Captura excepciones no controladas
        logger.error(f"Middleware error inesperado en {endpoint}: {exc}", exc_info=True)
        response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
        status_code = 500
    finally:
        # --- Lógica de Métricas ---
        latency = time.time() - start_time
        final_status_code = getattr(response, 'status_code', status_code)
        REQUEST_LATENCY.labels(endpoint=endpoint).observe(latency)
        REQUEST_COUNT.labels(
            method=request.method,
            endpoint=endpoint,
            status_code=final_status_code
        ).inc()

    if response is None: # Aseguramos que siempre haya una respuesta
         response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

    return response


# --- Dependencia de Seguridad ---

async def get_current_user_id(request: Request) -> int:
    """
    Dependencia de FastAPI que extrae el user_id verificado por el middleware.
    Se usa en todos los endpoints protegidos.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id is None:
        # Esto no debería pasar si el middleware funciona, pero es una doble verificación.
        logger.error(f"Error crítico: get_current_user_id llamado en una ruta sin user_id autenticado ({request.url.path})")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "User ID no disponible")
    return user_id


# --- Endpoints de Salud y Métricas ---
@app.get("/metrics", tags=["Monitoring"])
def metrics():
    """Expone métricas de la aplicación para Prometheus."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health", tags=["Monitoring"])
def health_check():
    """Verifica la salud básica del servicio Gateway."""
    return {"status": "ok", "service": "gateway_service"}

# --- Funciones Auxiliares para Proxy ---
async def forward_request(request: Request, target_url: str, inject_user_id: bool = False, pass_headers: list = []):
    """Función genérica para reenviar peticiones a servicios internos."""
    user_id = getattr(request.state, "user_id", None)
    
    payload = None
    headers_to_forward = {}

    for header_name in pass_headers:
        header_value = request.headers.get(header_name)
        if header_value:
            headers_to_forward[header_name] = header_value

    if user_id:
        headers_to_forward["X-User-Id"] = str(user_id)

    try:
        if request.method in ["POST", "PUT", "PATCH"]:
            content_type = request.headers.get("content-type", "").lower()
            
            if "application/json" in content_type:
                payload = await request.json()
                if inject_user_id:
                    if not user_id:
                        logger.error(f"Intento de inyectar user_id NULO en {target_url}")
                        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno: user_id no disponible")
                    payload['user_id'] = user_id
                response = await client.request(request.method, target_url, json=payload, headers=headers_to_forward)
            
            elif "application/x-www-form-urlencoded" in content_type:
                form_data = await request.form()
                response = await client.request(request.method, target_url, data=form_data, headers=headers_to_forward)
            
            else: 
                response = await client.request(request.method, target_url, content=await request.body(), headers=headers_to_forward)
        else: # GET, DELETE, etc.
            response = await client.request(request.method, target_url, headers=headers_to_forward)

        # Reenviar la respuesta (JSON o texto)
        try:
            response_json = response.json()
            return JSONResponse(status_code=response.status_code, content=response_json)
        except json.JSONDecodeError:
            return Response(status_code=response.status_code, content=response.text)

    except httpx.ConnectError as e:
        logger.error(f"Error de conexión al reenviar a {target_url}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Servicio interno no disponible: {target_url}")
    except httpx.HTTPStatusError as e:
        # Propaga el error del servicio interno
        logger.warning(f"Servicio interno {target_url} devolvió error {e.response.status_code}: {e.response.text}")
        return Response(status_code=e.response.status_code, content=e.response.content)
    except Exception as e:
        logger.error(f"Error inesperado al reenviar a {target_url}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno del Gateway")


# --- Endpoints Públicos (Proxy para Auth) ---

@app.post("/auth/register", tags=["Authentication"])
async def proxy_register(request: Request):
    """Reenvía la solicitud de registro al servicio de autenticación."""
    logger.info("Proxying request to /auth/register")
    return await forward_request(request, f"{AUTH_URL}/register")

@app.post("/auth/login", tags=["Authentication"])
async def proxy_login(request: Request):
    """Reenvía la solicitud de login (form-data) al servicio de autenticación."""
    logger.info("Proxying request to /auth/login")
    return await forward_request(request, f"{AUTH_URL}/login")

# --- Endpoints Privados (Proxy para Balance) ---

@app.get("/balance/me", tags=["Balance"])
async def proxy_get_my_balance(request: Request, user_id: int = Depends(get_current_user_id)):
    """Obtiene el saldo del usuario autenticado."""
    logger.info(f"Proxying request to /balance/{user_id}")
    return await forward_request(request, f"{BALANCE_URL}/balance/{user_id}")

# --- Endpoints Privados (Proxy para Ledger) ---

@app.post("/ledger/deposit", tags=["Ledger"])
async def proxy_deposit(request: Request, user_id: int = Depends(get_current_user_id)):
    """Reenvía la solicitud de depósito al servicio de ledger, inyectando user_id."""
    logger.info(f"Proxying request to /ledger/deposit for user_id: {user_id}")
    return await forward_request(request, f"{LEDGER_URL}/deposit", inject_user_id=True, pass_headers=["Idempotency-Key", "Authorization"])

@app.post("/ledger/transfer", tags=["Ledger"])
async def proxy_transfer(request: Request, user_id: int = Depends(get_current_user_id)):
    """Reenvía la solicitud de transferencia al servicio de ledger, inyectando user_id."""
    logger.info(f"Proxying request to /ledger/transfer for user_id: {user_id}")
    return await forward_request(request, f"{LEDGER_URL}/transfer", inject_user_id=True, pass_headers=["Idempotency-Key", "Authorization"])

@app.post("/ledger/contribute", tags=["Ledger"])
async def proxy_contribute(request: Request, user_id: int = Depends(get_current_user_id)):
    """Reenvía la solicitud de aporte a grupo al servicio de ledger, inyectando user_id."""
    logger.info(f"Proxying request to /ledger/contribute for user_id: {user_id}")
    return await forward_request(request, f"{LEDGER_URL}/contribute", inject_user_id=True, pass_headers=["Idempotency-Key", "Authorization"])

# ... (después de @app.post("/ledger/contribute") ...)

@app.post("/ledger/transfer/p2p", tags=["Ledger"])
async def proxy_transfer_p2p(request: Request, user_id: int = Depends(get_current_user_id)):
    """Reenvía la solicitud de transferencia P2P, inyectando el user_id (remitente)."""
    logger.info(f"Proxying request to /ledger/transfer/p2p for user_id: {user_id}")
    return await forward_request(
        request, 
        f"{LEDGER_URL}/transfer/p2p", 
        inject_user_id=True,
        pass_headers=["Idempotency-Key", "Authorization"]
    )


@app.get("/ledger/transactions/me", tags=["Ledger"])
async def proxy_get_my_transactions(request: Request, user_id: int = Depends(get_current_user_id)):
    """Obtiene el historial de movimientos del usuario autenticado."""
    logger.info(f"Proxying request to /ledger/transactions/me for user_id: {user_id}")

    
    # El user_id se pasa por el header X-User-ID
    return await forward_request(
        request, 
        f"{LEDGER_URL}/transactions/me",
        pass_headers=["Authorization"]
    )

# --- Endpoints Privados (Proxy para Group) ---

@app.post("/groups", status_code=status.HTTP_201_CREATED, tags=["Groups"])
async def proxy_create_group(request: Request, user_id: int = Depends(get_current_user_id)):
    """
    Reenvía la solicitud de creación de grupo al servicio de grupos,
    inyectando el user_id del token verificado.
    """
    logger.info(f"Proxying request to /groups for user_id: {user_id}")
    return await forward_request(
        request, 
        f"{GROUP_URL}/groups", 
        inject_user_id=False, 
        pass_headers=["Authorization"]
    )

@app.post("/groups/{group_id}/invite", tags=["Groups"])
async def proxy_invite_member(group_id: int, request: Request, user_id: int = Depends(get_current_user_id)):
    """Reenvía la solicitud de invitación de miembro al servicio de grupos."""
    logger.info(f"Proxying request to /groups/{group_id}/invite for user_id: {user_id}")
    return await forward_request(
        request, 
        f"{GROUP_URL}/groups/{group_id}/invite", 
        inject_user_id=False, 
        pass_headers=["Authorization"]
    )

@app.get("/groups/{group_id}", tags=["Groups"])
async def proxy_get_group(group_id: int, request: Request, user_id: int = Depends(get_current_user_id)):
    """Reenvía la solicitud para obtener detalles de un grupo."""
    logger.info(f"Proxying request to /groups/{group_id} for user_id: {user_id}")
    return await forward_request(
        request, 
        f"{GROUP_URL}/groups/{group_id}", 
        inject_user_id=False, 
        pass_headers=["Authorization"]
    )



@app.get("/groups/me", tags=["Groups"])
async def proxy_get_my_groups(request: Request, user_id: int = Depends(get_current_user_id)):
    """Obtiene los grupos del usuario autenticado."""
    logger.info(f"Proxying request to /groups/me for user_id: {user_id}")

   
    return await forward_request(
        request, 
        f"{GROUP_URL}/groups/me",
        inject_user_id=False, 
        pass_headers=["Authorization"]
    )

# --- Manejador de Cierre ---
@app.on_event("shutdown")
async def shutdown_event():
    """Cierra el cliente HTTP al apagar la aplicación."""
    await client.aclose()
    logger.info("Cliente HTTP del Gateway cerrado.")