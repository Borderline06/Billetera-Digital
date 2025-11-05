# tests/test_groups.py
"""Pruebas automatizadas para el flujo de Billeteras Grupales (BDG)."""

import requests
import pytest
import uuid
import time

# Importar la URL base y fixtures desde conftest
from conftest import GATEWAY_URL, test_user_token, auth_headers

# --- Función Auxiliar (la misma de test_ledger.py) ---
def get_current_balance(headers: dict) -> float:
    """Obtiene el saldo BDI actual del usuario autenticado."""
    balance_url = f"{GATEWAY_URL}/balance/me"
    try:
        r = requests.get(balance_url, headers=headers, timeout=10)
        r.raise_for_status()
        return float(r.json()["balance"])
    except Exception as e:
        pytest.fail(f"Fallo al obtener saldo BDI actual: {e}")
        return 0.0

def get_group_balance(group_id: int, headers: dict) -> float:
    """Obtiene el saldo BDG actual del grupo."""
    # Asumimos que no hay un endpoint de Gateway para esto,
    # así que llamamos directo a balance_service (solo para la prueba).
    # En producción, /balance/me podría devolver también los saldos grupales.

    # NOTA: Esta prueba requiere que balance_service esté expuesto
    # en el puerto 8003 para poder verificar el saldo del grupo.
    BALANCE_SERVICE_URL = "http://localhost:8003"
    group_balance_url = f"{BALANCE_SERVICE_URL}/group_balance/{group_id}"

    try:
        # Esta llamada NO pasa por el Gateway, no necesita auth_headers
        r = requests.get(group_balance_url, timeout=10)
        r.raise_for_status()
        return float(r.json()["balance"])
    except Exception as e:
        pytest.fail(f"Fallo al obtener saldo BDG (directo a balance_service): {e}")
        return 0.0

# --- Pruebas del Flujo Grupal ---

@pytest.fixture(scope="module")
def setup_funds(auth_headers):
    """
    Fixture de módulo: Deposita fondos una vez para todas las pruebas en este archivo.
    """
    print("\n[Fixture BDG] Depositando fondos para pruebas de grupos...")
    deposit_url = f"{GATEWAY_URL}/ledger/deposit"
    deposit_amount = 500.0
    deposit_key = str(uuid.uuid4())
    headers = {**auth_headers, "Idempotency-Key": deposit_key}
    payload = {"amount": deposit_amount}

    try:
        r = requests.post(deposit_url, json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        initial_balance = get_current_balance(auth_headers)
        assert initial_balance >= deposit_amount
        print(f"[Fixture BDG] Fondos depositados. Saldo BDI actual: {initial_balance}")
        return initial_balance
    except Exception as e:
        pytest.fail(f"Fallo en fixture BDG: No se pudo depositar fondos. Error: {e}")

@pytest.fixture(scope="module")
def created_group(auth_headers) -> int:
    """
    Fixture de módulo: Crea un grupo una vez para todas las pruebas en este archivo.
    Devuelve el ID del grupo creado.
    """
    print("\n[Fixture BDG] Creando grupo de prueba...")
    group_url = f"{GATEWAY_URL}/groups"
    group_name = f"Grupo de Prueba {uuid.uuid4()}"
    payload = {"name": group_name}

    try:
        r = requests.post(group_url, json=payload, headers=auth_headers, timeout=15)
        r.raise_for_status()
        group_data = r.json()
        group_id = group_data.get("id")
        assert group_id is not None
        print(f"[Fixture BDG] Grupo '{group_name}' (ID: {group_id}) creado.")
        return group_id
    except Exception as e:
        pytest.fail(f"Fallo en fixture BDG: No se pudo crear el grupo. Error: {e}")

def test_group_creation(created_group, auth_headers, test_user_token):
    """
    Verifica que el grupo se creó correctamente y el líder es miembro.
    """
    print(f"\n[Test] Verificando creación del grupo ID: {created_group}...")
    group_url = f"{GATEWAY_URL}/groups/{created_group}"

    r = requests.get(group_url, headers=auth_headers, timeout=10)
    r.raise_for_status()
    group_data = r.json()

    assert group_data["id"] == created_group
    assert group_data["leader_user_id"] == test_user_token["user_id"]
    assert len(group_data["members"]) == 1, "El grupo debe tener 1 miembro (el líder) al crearse"
    assert group_data["members"][0]["user_id"] == test_user_token["user_id"]
    assert group_data["members"][0]["role"] == "leader", "El creador debe tener rol 'leader'"
    print(f"[Test] Creación de grupo verificada.")

def test_group_contribution(setup_funds, created_group, auth_headers, idempotency_key):
    """
    Verifica el flujo de aporte BDI -> BDG.
    1. Obtiene saldo BDI y BDG iniciales.
    2. Realiza un aporte (/ledger/contribute).
    3. Verifica que el aporte fue 'COMPLETED'.
    4. Verifica que el saldo BDI se redujo.
    5. Verifica que el saldo BDG aumentó.
    """
    contribute_url = f"{GATEWAY_URL}/ledger/contribute"
    contribution_amount = 75.50
    group_id = created_group

    headers = {**auth_headers, "Idempotency-Key": idempotency_key}
    payload = {
        "group_id": group_id,
        "amount": contribution_amount
        # user_id (quien aporta) es inyectado por el Gateway
    }

    print(f"\n[Test] Aporte BDG: Probando aporte de {contribution_amount} al grupo {group_id}...")

    try:
        # 1. Obtener saldos iniciales
        initial_bdi_balance = get_current_balance(auth_headers)
        initial_bdg_balance = get_group_balance(group_id, auth_headers)
        print(f"[Test] Aporte BDG: Saldo BDI inicial = {initial_bdi_balance}")
        print(f"[Test] Aporte BDG: Saldo BDG inicial = {initial_bdg_balance}")

        assert initial_bdi_balance >= contribution_amount, "Fondos BDI insuficientes para iniciar la prueba"

        # 2. Realizar el aporte
        r_contribute = requests.post(contribute_url, json=payload, headers=headers, timeout=15)
        r_contribute.raise_for_status()
        tx_data = r_contribute.json()
        print(f"[Test] Aporte BDG: Respuesta recibida -> {tx_data}")

        # 3. Verificar transacción
        assert tx_data.get("status") == "COMPLETED", "El estado del aporte debe ser 'COMPLETED'"
        assert tx_data.get("type") == "CONTRIBUTION", "El tipo de tx debe ser 'CONTRIBUTION'"
        assert tx_data.get("amount") == contribution_amount

        # 4. Verificar saldo BDI final (reducción)
        final_bdi_balance = get_current_balance(auth_headers)
        expected_bdi_balance = initial_bdi_balance - contribution_amount
        print(f"[Test] Aporte BDG: Saldo BDI final = {final_bdi_balance} (Esperado: {expected_bdi_balance})")
        assert final_bdi_balance == pytest.approx(expected_bdi_balance), "El saldo BDI no se redujo correctamente."

        # 5. Verificar saldo BDG final (aumento)
        # Damos 1 segundo para asegurar que la transacción se refleje
        time.sleep(1) 
        final_bdg_balance = get_group_balance(group_id, auth_headers)
        expected_bdg_balance = initial_bdg_balance + contribution_amount
        print(f"[Test] Aporte BDG: Saldo BDG final = {final_bdg_balance} (Esperado: {expected_bdg_balance})")
        assert final_bdg_balance == pytest.approx(expected_bdg_balance), "El saldo BDG no aumentó correctamente."

        print(f"[Test] Aporte BDG: ¡Flujo completado y verificado exitosamente!")

    except requests.exceptions.Timeout:
        pytest.fail(f"Fallo en prueba de aporte BDG: Timeout al llamar a {contribute_url}.")
    except requests.exceptions.RequestException as e:
        error_text = e.response.text if e.response else "Sin respuesta"
        pytest.fail(f"Fallo en prueba de aporte BDG: Error en {contribute_url}. Status: {e.response.status_code if e.response else 'N/A'}. Error: {e}\nRespuesta: {error_text}")
    except AssertionError as e:
        pytest.fail(f"Fallo en prueba de aporte BDG: Verificación fallida - {e}")
    except Exception as e:
        pytest.fail(f"Fallo inesperado en prueba de aporte BDG: {e}")