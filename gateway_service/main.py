"""API Gateway para Pixel Money. Punto de entrada único, maneja autenticación y enrutamiento."""

import os
import httpx
import logging
import time
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse, Response
from dotenv import load_dotenv
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

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
    # El gateway no puede funcionar sin estas URLs. Podríamos salir.
    # exit(1)

# Inicializa FastAPI
app = FastAPI(
    title="API Gateway - Pixel Money",
    description="Punto de entrada único para todos los servicios de la billetera digital.",
    version="1.0.0"
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
# Usar un cliente persistente mejora el rendimiento
client = httpx.AsyncClient(timeout=15.0) # Timeout general para llamadas internas

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
    status_code = 500 # Default
    user_id = None # Para métricas/logs si es relevante

    endpoint = request.url.path # Guardamos antes de posibles errores

    try:
        # --- Lógica de Seguridad (adaptada del middleware original) ---
        request.state.user_id = None # Inicializar
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
                user_id_str = token_payload.get("sub")
                if user_id_str:
                    user_id = int(user_id_str)
                    request.state.user_id = user_id # Inyectamos user_id para los endpoints
                else:
                    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Payload del token inválido (sin 'sub')")

            except httpx.RequestError:
                 raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Servicio de autenticación no disponible")
            except ValueError: # Error al convertir user_id_str a int
                 raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Payload del token inválido ('sub' no es un ID válido)")
            except Exception as auth_exc: # Captura otros errores de validación
                 logger.error(f"Error inesperado en validación de token: {auth_exc}", exc_info=True)
                 raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Error en validación de token")

        # --- Llamar al siguiente middleware o endpoint ---
        response = await call_next(request)
        status_code = response.status_code

    except HTTPException as http_exc:
        # Captura errores HTTP lanzados por el middleware de seguridad o el endpoint
        status_code = http_exc.status_code
        response = JSONResponse(status_code=status_code, content={"detail": http_exc.detail})
        # No relanzamos, devolvemos la respuesta JSON directamente
    except Exception as exc:
        # Captura excepciones no controladas
        logger.error(f"Middleware error inesperado en {endpoint}: {exc}", exc_info=True)
        response = Response("Internal Server Error", status_code=500)
        status_code = 500
    finally:
        # --- Lógica de Métricas ---
        latency = time.time() - start_time
        # Normalizar endpoint si es necesario (ej. quitar IDs)
        # ... (lógica de normalización si se añade) ...
        final_status_code = getattr(response, 'status_code', status_code) # Usamos getattr por si response es None
        REQUEST_LATENCY.labels(endpoint=endpoint).observe(latency)
        REQUEST_COUNT.labels(
            method=request.method,
            endpoint=endpoint,
            status_code=final_status_code
        ).inc()

    # Aseguramos devolver una respuesta, incluso si es la de error creada aquí
    if response is None:
        logger.error(f"Middleware finalizó sin respuesta para {endpoint}")
        response = Response("Internal Server Error", status_code=500)

    return response


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
    
    # Prepara payload y headers para reenviar
    payload = None
    headers_to_forward = {}

    # Reenvía cabeceras específicas si se indican
    for header_name in pass_headers:
        header_value = request.headers.get(header_name)
        if header_value:
            headers_to_forward[header_name] = header_value

    # Lee el cuerpo según el método
    if request.method in ["POST", "PUT", "PATCH"]:
        content_type = request.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            payload = await request.json()
            if inject_user_id:
                if not user_id:
                    logger.error(f"Intento de inyectar user_id NULO en {target_url}")
                    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno: user_id no disponible")
                payload['user_id'] = user_id # Inyecta user_id validado
            response = await client.request(request.method, target_url, json=payload, headers=headers_to_forward)
        elif "application/x-www-form-urlencoded" in content_type:
            form_data = await request.form()
            response = await client.request(request.method, target_url, data=form_data, headers=headers_to_forward)
        else: # Otros tipos de contenido no soportados directamente
             response = await client.request(request.method, target_url, content=await request.body(), headers=headers_to_forward)
    else: # GET, DELETE, etc.
        response = await client.request(request.method, target_url, headers=headers_to_forward)

    # Devuelve la respuesta del servicio interno al cliente original
    return JSONResponse(status_code=response.status_code, content=response.json())


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
async def proxy_get_my_balance(request: Request):
    """Obtiene el saldo del usuario autenticado."""
    # El middleware ya validó el token y puso user_id en request.state
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "User ID not available after authentication")
    
    logger.info(f"Proxying request to /balance/{user_id}")
    return await forward_request(request, f"{BALANCE_URL}/balance/{user_id}")

# --- Endpoints Privados (Proxy para Ledger) ---

@app.post("/ledger/deposit", tags=["Ledger"])
async def proxy_deposit(request: Request):
    """Reenvía la solicitud de depósito al servicio de ledger, inyectando user_id."""
    logger.info("Proxying request to /ledger/deposit")
    # Pasa 'Idempotency-Key' y reenvía 'Authorization' (opcional, por si ledger lo necesita)
    return await forward_request(request, f"{LEDGER_URL}/deposit", inject_user_id=True, pass_headers=["Idempotency-Key", "Authorization"])

@app.post("/ledger/transfer", tags=["Ledger"])
async def proxy_transfer(request: Request):
    """Reenvía la solicitud de transferencia al servicio de ledger, inyectando user_id."""
    logger.info("Proxying request to /ledger/transfer")
    return await forward_request(request, f"{LEDGER_URL}/transfer", inject_user_id=True, pass_headers=["Idempotency-Key", "Authorization"])

@app.post("/ledger/contribute", tags=["Ledger"])
async def proxy_contribute(request: Request):
    """Reenvía la solicitud de aporte a grupo al servicio de ledger, inyectando user_id."""
    logger.info("Proxying request to /ledger/contribute")
    return await forward_request(request, f"{LEDGER_URL}/contribute", inject_user_id=True, pass_headers=["Idempotency-Key", "Authorization"])

# --- Endpoints Privados (Proxy para Group) ---

@app.post("/groups", tags=["Groups"])
async def proxy_create_group(request: Request):
    """Reenvía la solicitud de creación de grupo al servicio de grupos."""
    logger.info("Proxying request to /groups")
    # Pasa la cabecera Authorization por si group_service la necesita
    return await forward_request(request, f"{GROUP_URL}/groups", inject_user_id=False, pass_headers=["Authorization"])

@app.post("/groups/{group_id}/invite", tags=["Groups"])
async def proxy_invite_member(group_id: int, request: Request):
    """Reenvía la solicitud de invitación de miembro al servicio de grupos."""
    logger.info(f"Proxying request to /groups/{group_id}/invite")
    return await forward_request(request, f"{GROUP_URL}/groups/{group_id}/invite", inject_user_id=False, pass_headers=["Authorization"])

@app.get("/groups/{group_id}", tags=["Groups"])
async def proxy_get_group(group_id: int, request: Request):
    """Reenvía la solicitud para obtener detalles de un grupo."""
    logger.info(f"Proxying request to /groups/{group_id}")
    return await forward_request(request, f"{GROUP_URL}/groups/{group_id}", inject_user_id=False, pass_headers=["Authorization"])

# --- Manejador de Cierre ---
@app.on_event("shutdown")
async def shutdown_event():
    """Cierra el cliente HTTP al apagar la aplicación."""
    await client.aclose()
    logger.info("Cliente HTTP del Gateway cerrado.")