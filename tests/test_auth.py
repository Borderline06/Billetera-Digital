# tests/test_auth.py
import requests
import uuid
from .conftest import GATEWAY_URL # Importamos la URL base

def test_register_duplicate_email(test_user_token):
    """
    Verifica que no se puede registrar un usuario con un email existente.
    Usa el usuario creado en conftest.
    """
    # Intentamos registrar DE NUEVO el mismo usuario
    register_payload = {"email": test_user_token['email'], "password": "newpassword"}
    r = requests.post(f"{GATEWAY_URL}/auth/register", json=register_payload)
    
    # Esperamos un error 400 (Bad Request) o 409 (Conflict)
    assert r.status_code in [400, 409], f"Esperado 400/409 pero se obtuvo {r.status_code}"

def test_login_invalid_credentials():
    """
    Verifica que el login falla con contraseña incorrecta.
    """
    login_payload = {"username": f"nouser_{uuid.uuid4()}@example.com", "password": "wrongpassword"}
    r = requests.post(f"{GATEWAY_URL}/auth/login", data=login_payload)
    
    # Esperamos un error 401 (Unauthorized)
    assert r.status_code == 401, f"Esperado 401 pero se obtuvo {r.status_code}"