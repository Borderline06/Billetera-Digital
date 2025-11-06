# group_service/main.py (Versión Corregida y Completa)

"""Servicio FastAPI para gestionar grupos (Billeteras Digitales Grupales - BDG)."""

import logging
import time
import httpx
import models
import os
import schemas # Asegúrate que schemas.py esté en la misma carpeta
from fastapi import FastAPI, Depends, HTTPException, status, Request, Header
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from fastapi.responses import Response, JSONResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from sqlalchemy.orm import joinedload


# Importaciones locales (absolutas)
from db import engine, Base, get_db, SessionLocal # Importar SessionLocal para health check
from models import Group, GroupMember, GroupRole
from dotenv import load_dotenv
load_dotenv()


# Configura logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Lee la URL de Balance Service del .env
BALANCE_SERVICE_URL = os.getenv("BALANCE_SERVICE_URL")
if not BALANCE_SERVICE_URL:
    logger.critical("¡Variable de entorno BALANCE_SERVICE_URL no definida! El servicio fallará al crear grupos.")
    # No detenemos el arranque, pero lo advertimos fuertemente


# Crea tablas si no existen al iniciar
try:
    Base.metadata.create_all(bind=engine)
    logger.info("Tablas de base de datos (groups, group_members) verificadas/creadas.")
except Exception as e:
    logger.error(f"Error al inicializar la base de datos: {e}", exc_info=True)
    # Considerar detener el servicio

# Inicializa FastAPI
app = FastAPI(
    title="Group Service - Pixel Money",
    description="Gestiona la creación, membresía y detalles de Billeteras Digitales Grupales (BDG).",
    version="1.0.0"
)

# --- Métricas Prometheus ---
REQUEST_COUNT = Counter(
    "group_requests_total",
    "Total requests processed by Group Service",
    ["method", "endpoint", "status_code"]
)
REQUEST_LATENCY = Histogram(
    "group_request_latency_seconds",
    "Request latency in seconds for Group Service",
    ["endpoint"]
)
GROUP_CREATED_COUNT = Counter(
    "group_groups_created_total",
    "Número total de grupos creados"
)
MEMBER_INVITED_COUNT = Counter(
    "group_members_invited_total",
    "Número total de miembros invitados"
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
        # Devolvemos JSONResponse para que el cliente entienda el error
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
    finally:
        latency = time.time() - start_time
        endpoint = request.url.path
        
        # Normalizar endpoints con IDs (ej /groups/1 -> /groups/{group_id})
        parts = endpoint.split("/")
        if len(parts) > 2 and parts[1] == "groups" and parts[2].isdigit():
             if len(parts) > 3: # ej /groups/1/invite
                 endpoint = f"/groups/{{group_id}}/{parts[3]}"
             else: # ej /groups/1
                 endpoint = "/groups/{group_id}"

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
        db.execute(text("SELECT 1")) # Chequeo rápido a la BD
        db.close()
    except Exception as e:
        logger.error(f"Health check fallido - Error de BD: {e}", exc_info=True)
        db_status = "error"
        # Devolver 503 si la BD no está disponible
        raise HTTPException(status_code=503, detail=f"Database connection error: {e}")

    return {"status": "ok", "service": "group_service", "database": db_status}


# --- Endpoints de API para Grupos ---

@app.post("/groups", response_model=schemas.GroupResponse, status_code=status.HTTP_201_CREATED, tags=["Groups"])
def create_group(
    group_in: schemas.GroupCreate,
    x_user_id: int = Header(..., alias="X-User-ID"), # <-- ¡LEE EL HEADER!
    db: Session = Depends(get_db)
):
    """
    Crea un nuevo grupo (BDG).
    El usuario autenticado (del header X-User-ID) se convierte en el líder.
    """
    
    leader_user_id = x_user_id # <-- ¡USA EL HEADER!
    if not leader_user_id:
         raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "User ID no autenticado (no se encontró X-User-ID)")

    logger.info(f"Usuario {leader_user_id} creando grupo con nombre: {group_in.name}")
    new_group = models.Group(name=group_in.name, leader_user_id=leader_user_id)

    try:
        db.add(new_group)
        db.flush() # Obtenemos el ID del grupo

        leader_member = models.GroupMember(
            group_id=new_group.id,
            user_id=leader_user_id,
            role=models.GroupRole.LEADER
        )
        db.add(leader_member)

        # --- Llamar a balance_service para crear la cuenta grupal ---
        logger.info(f"Creando cuenta de balance para group_id: {new_group.id}")
        if not BALANCE_SERVICE_URL:
            logger.error("¡BALANCE_SERVICE_URL no está configurada! No se puede crear cuenta de grupo.")
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Configuración del servicio incompleta.")
            
        try:
            with httpx.Client() as client:
                response = client.post(
                    f"{BALANCE_SERVICE_URL}/group_accounts",
                    json={"group_id": new_group.id}
                )
                response.raise_for_status() 
        except httpx.RequestError as e:
            logger.error(f"Error al crear cuenta de balance para grupo {new_group.id}: {e}")
            db.rollback()
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Error al contactar Balance Service al crear grupo")
        except httpx.HTTPStatusError as e:
            logger.error(f"Balance Service devolvió error al crear cuenta para grupo {new_group.id}: {e.response.text}")
            db.rollback()
            raise HTTPException(status_code=e.response.status_code, detail=f"Balance Service: {e.response.json().get('detail', 'Error')}")
        # --- Fin llamada a balance_service ---

        db.commit() # Commit final de grupo, miembro y cuenta
        db.refresh(new_group) 
        logger.info(f"Grupo ID {new_group.id} ('{new_group.name}') creado exitosamente por user_id: {leader_user_id}")
        GROUP_CREATED_COUNT.inc()
        return new_group

    except IntegrityError: 
        db.rollback()
        logger.warning(f"Error de integridad al crear grupo '{group_in.name}' por user {leader_user_id}.")
        raise HTTPException(status.HTTP_409_CONFLICT, "Conflicto al crear el grupo, posible duplicado o dato inválido.")
    except HTTPException as http_exc:
        db.rollback() # Asegúrate de hacer rollback si httpx falló
        raise http_exc # Relanza el error
    except Exception as e:
        db.rollback()
        logger.error(f"Error interno al crear grupo '{group_in.name}': {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno del servidor al crear grupo.")


@app.post("/groups/{group_id}/invite", response_model=schemas.GroupMemberResponse, status_code=status.HTTP_201_CREATED, tags=["Groups"])
def invite_member(
    group_id: int, 
    invite_in: schemas.GroupInviteRequest, 
    x_user_id: int = Header(..., alias="X-User-ID"), # <-- ¡LEE EL HEADER!
    db: Session = Depends(get_db)
):
    """
    Añade un usuario (por ID) como miembro a un grupo existente.
    Requiere que el solicitante (X-User-ID) sea el líder del grupo.
    """
    requesting_user_id = x_user_id # <-- ¡USA EL HEADER!
    user_to_invite_id = invite_in.user_id_to_invite

    if not requesting_user_id:
         raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "User ID no autenticado (no se encontró X-User-ID)")

    logger.info(f"Usuario {requesting_user_id} intentando invitar a user_id {user_to_invite_id} al grupo {group_id}")

    # 1. Verificar grupo y permisos del solicitante
    group = db.query(models.Group).filter(models.Group.id == group_id).first()
    if not group:
        logger.warning(f"Intento de invitar a grupo inexistente: {group_id}")
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Grupo con id {group_id} no encontrado.")

    if group.leader_user_id != requesting_user_id:
        logger.warning(f"Intento no autorizado de invitar al grupo {group_id} por user {requesting_user_id} (no es líder).")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo el líder del grupo puede invitar miembros.")

    # 2. Verificar si el invitado ya es miembro
    existing_member = db.query(models.GroupMember).filter(
        models.GroupMember.group_id == group_id,
        models.GroupMember.user_id == user_to_invite_id
    ).first()
    
    if existing_member:
        logger.warning(f"Intento de invitar a usuario {user_to_invite_id} que ya es miembro del grupo {group_id}.")
        raise HTTPException(status.HTTP_409_CONFLICT, "El usuario ya es miembro de este grupo.")

    # 3. Añadir nuevo miembro
    new_member = models.GroupMember(
        group_id=group_id,
        user_id=user_to_invite_id,
        role=models.GroupRole.MEMBER
    )

    try:
        db.add(new_member)
        db.commit()
        db.refresh(new_member)
        logger.info(f"Usuario {user_to_invite_id} añadido exitosamente al grupo {group_id} como miembro.")
        MEMBER_INVITED_COUNT.inc() # Incrementa métrica
        return new_member
    except Exception as e:
        db.rollback()
        logger.error(f"Error interno al añadir miembro al grupo {group_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno al invitar al miembro.")


@app.get("/groups/{group_id}", response_model=schemas.GroupResponse, tags=["Groups"])
def get_group_details(
    group_id: int,
    x_user_id: int = Header(..., alias="X-User-ID"), # <-- ¡LEE EL HEADER!
    db: Session = Depends(get_db)
):
    """
    Obtiene los detalles de un grupo específico, incluyendo la lista de miembros.
    Requiere que el solicitante (X-User-ID) sea miembro del grupo.
    """
    requesting_user_id = x_user_id # <-- ¡USA EL HEADER!
    if not requesting_user_id:
        logger.error("Error crítico: User ID no encontrado en header X-User-ID")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "User ID no autenticado")

    logger.debug(f"Usuario {requesting_user_id} solicitando detalles del grupo {group_id}")

    # Carga el grupo y sus miembros en una sola consulta
    group = db.query(models.Group).options(joinedload(models.Group.members)).filter(models.Group.id == group_id).first()

    if not group:
        logger.warning(f"Usuario {requesting_user_id} solicitó grupo inexistente: {group_id}")
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Grupo con id {group_id} no encontrado.")

    # Verificar si el solicitante es miembro
    is_member = any(member.user_id == requesting_user_id for member in group.members)

    if not is_member:
         logger.warning(f"Acceso denegado: Usuario {requesting_user_id} intentó ver grupo {group_id} del que no es miembro.")
         raise HTTPException(status.HTTP_403_FORBIDDEN, "Acceso denegado. No eres miembro de este grupo.")

    logger.info(f"Devolviendo detalles del grupo {group_id} a user_id {requesting_user_id}")
    return group