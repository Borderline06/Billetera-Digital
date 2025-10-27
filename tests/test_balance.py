# tests/test_balance.py
import requests
from .conftest import GATEWAY_URL

def test_get_initial_balance(auth_headers):
    """
    Verifies that a newly registered user starts with a balance of 0.0.
    Uses the token from conftest (auth_headers fixture).
    """
    try:
        r = requests.get(f"{GATEWAY_URL}/balance/me", headers=auth_headers)
        r.raise_for_status() # Fail if status code is not 2xx
        account_data = r.json()
        
        # Check the structure and initial balance
        assert "user_id" in account_data
        assert "balance" in account_data
        assert "currency" in account_data
        assert account_data["balance"] == 0.0, "Initial balance should be 0.0"
        assert account_data["currency"] == "USD", "Default currency should be USD"
        
    except requests.exceptions.RequestException as e:
        pytest.fail(f"Failed to get initial balance: {e}")