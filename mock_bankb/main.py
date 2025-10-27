# mock_bankb/main.py
import uuid
from fastapi import FastAPI, HTTPException, status
from . import schemas

app = FastAPI(title="Mock Bank B")

@app.post("/receive")
def receive_transfer(payload: schemas.ExternalTransfer):
    """
    Simula la recepción de una transferencia de un banco externo.
    Aplica reglas de negocio simples.
    """
    
    # Regla 1: Rechazar montos demasiado altos
    if payload.amount > 10000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Límite de transferencia externa excedido ($10,000)"
        )
    
    # Regla 2: Rechazar cuentas de destino "falsas" (simulación)
    if payload.to_account == "fake@bankb.com":
         raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cuenta de destino no encontrada en Banco B"
        )

    # Si todo está bien, aceptamos la transferencia
    print(f"Mock Bank B: Recibida transferencia de {payload.amount} para {payload.to_account}")
    
    return {
        "status": "ACCEPTED_BY_BANK_B",
        "remote_tx_id": str(uuid.uuid4()) # Devolvemos un ID de transacción falso
    }

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "mock_bank_b"}