# tests/conftest.py
import pytest
import requests
import uuid

GATEWAY_URL = "http://localhost:8080" # Puerto correcto del Gateway FASE 2

# Usamos un email único por cada ejecución para evitar conflictos
TEST_EMAIL = f"testuser_{uuid.uuid4()}@example.com"
TEST_PASSWORD = "password123"

@pytest.fixture(scope="session")
def test_user_token():
    """
    Fixture que se ejecuta una vez por sesión.
    1. Registra un nuevo usuario único.
    2. Inicia sesión para obtener un token.
    3. Devuelve el email y el token.
    """
    # --- Registro ---
    # Asumimos que auth_service FUE CORREGIDO para aceptar JSON
    register_payload = {"email": TEST_EMAIL, "password": TEST_PASSWORD}
    try:
        r_register = requests.post(f"{GATEWAY_URL}/auth/register", json=register_payload)
        r_register.raise_for_status() # Lanza excepción si el registro falla
        user_data = r_register.json()
        print(f"\nUsuario de prueba registrado: {TEST_EMAIL}")
    except requests.exceptions.RequestException as e:
        pytest.fail(f"Fallo al registrar usuario de prueba: {e}")

    # --- Login ---
    # Login usa form-data (coherente con FASE 2)
    login_payload = {"username": TEST_EMAIL, "password": TEST_PASSWORD}
    try:
        r_login = requests.post(f"{GATEWAY_URL}/auth/login", data=login_payload)
        r_login.raise_for_status()
        token_data = r_login.json()
        print(f"Login exitoso, token obtenido.")
    except requests.exceptions.RequestException as e:
        pytest.fail(f"Fallo al iniciar sesión con usuario de prueba: {e}")

    return {"email": TEST_EMAIL, "token": token_data.get("access_token")}

# Fixture de utilidad para las cabeceras de autorización
@pytest.fixture
def auth_headers(test_user_token):
    return {"Authorization": f"Bearer {test_user_token['token']}"}

# Fixture de utilidad para generar claves de idempotencia únicas
@pytest.fixture
def idempotency_key():
    return str(uuid.uuid4())