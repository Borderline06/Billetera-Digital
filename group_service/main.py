"""Servicio FastAPI para gestionar grupos (Billeteras Digitales Grupales - BDG)."""

import logging
import time
import httpx
import models
import os
from fastapi import FastAPI, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from fastapi.responses import Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST


# Importaciones locales (absolutas)
from db import engine, Base, get_db, SessionLocal # Importar SessionLocal para health check
from models import Group, GroupMember, GroupRole
from dotenv import load_dotenv
load_dotenv()
import schemas

# Configura logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BALANCE_SERVICE_URL = os.getenv("BALANCE_SERVICE_URL")

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
        return Response("Internal Server Error", status_code=500)
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

    return {"status": "ok", "service": "group_service", "database": db_status}


# --- Endpoints de API para Grupos ---

# Reemplaza la función create_group entera con esto:

@app.post("/groups", response_model=schemas.GroupResponse, status_code=status.HTTP_201_CREATED, tags=["Groups"])
def create_group(
    group_in: schemas.GroupCreate, # <-- Ahora recibe el user_id aquí
    db: Session = Depends(get_db)
):
    """
    Crea un nuevo grupo (BDG).
    El usuario autenticado (inyectado por el Gateway en el payload) se convierte en el líder.
    """

    # --- LÍNEA CORREGIDA ---
    # ¡Lee el user_id del payload (que inyectó el Gateway)!
    leader_user_id = group_in.user_id 
    # --- FIN LÍNEA CORREGIDA ---

    if not leader_user_id: # Doble chequeo por si acaso
         # Esto ahora es un error de validación, no de autenticación
         raise HTTPException(status.HTTP_400_BAD_REQUEST, "user_id es requerido en el payload")

    logger.info(f"Usuario {leader_user_id} creando grupo con nombre: {group_in.name}")
    # Asegúrate de que tu models.py se llame 'models' y esté importado
    new_group = models.Group(name=group_in.name, leader_user_id=leader_user_id)

    try:
        db.add(new_group)
        db.flush() # Obtenemos el ID del grupo

        # Añadimos al líder como el primer miembro
        leader_member = models.GroupMember(
            group_id=new_group.id,
            user_id=leader_user_id,
            role=models.GroupRole.LEADER # Asumiendo que tienes 'models.GroupRole'
        )
        db.add(leader_member)

        # --- ¡BUG SUTIL ARREGLADO! ---
        # Llamar a balance_service para crear la cuenta grupal
        logger.info(f"Creando cuenta de balance para group_id: {new_group.id}")
        try:
            # ¡Esta llamada es Sincrónica! No podemos usar 'await'.
            # Usamos httpx.Client() en lugar de AsyncClient
            with httpx.Client() as client:
                response = client.post(
                    f"{BALANCE_SERVICE_URL}/group_accounts", # Endpoint de creación de cuenta grupal
                    json={"group_id": new_group.id}
                )
                response.raise_for_status() # Lanza error si balance_service falla
        except httpx.RequestError as e:
            logger.error(f"Error al crear cuenta de balance para grupo {new_group.id}: {e}")
            db.rollback()
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Error al contactar Balance Service al crear grupo")
        except httpx.HTTPStatusError as e:
            logger.error(f"Balance Service devolvió error al crear cuenta para grupo {new_group.id}: {e.response.text}")
            db.rollback()
            raise HTTPException(status_code=e.response.status_code, detail=f"Balance Service: {e.response.json().get('detail', 'Error')}")
        # --- FIN BUG SUTIL ---

        db.commit() # Commit final de grupo, miembro y cuenta
        db.refresh(new_group) 
        logger.info(f"Grupo ID {new_group.id} ('{new_group.name}') creado exitosamente por user_id: {leader_user_id}")
        GROUP_CREATED_COUNT.inc()
        return new_group

    except IntegrityError: 
        db.rollback()
        logger.warning(f"Error de integridad al crear grupo '{group_in.name}' por user {leader_user_id}.")
        raise HTTPException(status.HTTP_409_CONFLICT, "Conflicto al crear el grupo, posible duplicado o dato inválido.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error interno al crear grupo '{group_in.name}': {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno del servidor al crear grupo.")


@app.post("/groups{group_id}/invite", response_model=schemas.GroupMemberResponse, status_code=status.HTTP_201_CREATED, tags=["Groups"])
def invite_member(group_id: int, invite_in: schemas.GroupInvite, request: Request, db: Session = Depends(get_db)):
    """
    Añade un usuario (por ID) como miembro a un grupo existente.
    Requiere que el solicitante sea el líder del grupo.
    """
    requesting_user_id = getattr(request.state, "user_id", None)
    if not requesting_user_id:
        logger.error("Error crítico: User ID no encontrado en request.state para ruta protegida /groups/invite")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "User ID no autenticado")

    logger.info(f"Usuario {requesting_user_id} intentando invitar a user_id {invite_in.user_id_to_invite} al grupo {group_id}")

    # 1. Verificar grupo y permisos del solicitante
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        logger.warning(f"Intento de invitar a grupo inexistente: {group_id}")
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Grupo con id {group_id} no encontrado.")

    if group.leader_user_id != requesting_user_id:
        logger.warning(f"Intento no autorizado de invitar al grupo {group_id} por user {requesting_user_id} (no es líder).")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo el líder del grupo puede invitar miembros.")

    # 2. Verificar si el invitado ya es miembro
    existing_member = db.query(GroupMember).filter(
        GroupMember.group_id == group_id,
        GroupMember.user_id == invite_in.user_id_to_invite
    ).first()
    if existing_member:
        logger.warning(f"Intento de invitar a usuario {invite_in.user_id_to_invite} que ya es miembro del grupo {group_id}.")
        raise HTTPException(status.HTTP_409_CONFLICT, "El usuario ya es miembro de este grupo.")

    # 3. Añadir nuevo miembro
    new_member = GroupMember(
        group_id=group_id,
        user_id=invite_in.user_id_to_invite,
        role=GroupRole.MEMBER # Invitados siempre son miembros normales
    )

    try:
        db.add(new_member)
        db.commit()
        db.refresh(new_member)
        logger.info(f"Usuario {invite_in.user_id_to_invite} añadido exitosamente al grupo {group_id} como miembro.")
        MEMBER_INVITED_COUNT.inc() # Incrementa métrica
        return new_member
    except Exception as e:
        db.rollback()
        logger.error(f"Error interno al añadir miembro al grupo {group_id}: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno al invitar al miembro.")


@app.get("/groups{group_id}", response_model=schemas.GroupResponse, tags=["Groups"])
def get_group_details(group_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Obtiene los detalles de un grupo específico, incluyendo la lista de miembros.
    Requiere que el solicitante sea miembro del grupo.
    """
    requesting_user_id = getattr(request.state, "user_id", None)
    if not requesting_user_id:
        logger.error("Error crítico: User ID no encontrado en request.state para ruta protegida /groups/{group_id}")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "User ID no autenticado")

    logger.debug(f"Usuario {requesting_user_id} solicitando detalles del grupo {group_id}")

    # Usamos options(joinedload(Group.members)) para cargar los miembros eficientemente en la misma consulta
    from sqlalchemy.orm import joinedload
    group = db.query(Group).options(joinedload(Group.members)).filter(Group.id == group_id).first()

    if not group:
        logger.warning(f"Usuario {requesting_user_id} solicitó grupo inexistente: {group_id}")
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Grupo con id {group_id} no encontrado.")

    # Verificar si el solicitante es miembro (cargado con joinedload)
    is_member = any(member.user_id == requesting_user_id for member in group.members)

    if not is_member:
         logger.warning(f"Acceso denegado: Usuario {requesting_user_id} intentó ver grupo {group_id} del que no es miembro.")
         raise HTTPException(status.HTTP_403_FORBIDDEN, "Acceso denegado. No eres miembro de este grupo.")

    logger.info(f"Devolviendo detalles del grupo {group_id} a user_id {requesting_user_id}")
    return group