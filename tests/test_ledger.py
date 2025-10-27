# tests/test_ledger.py
import requests
import pytest
import uuid
from .conftest import GATEWAY_URL

# Helper function to get current balance
def get_current_balance(headers):
    r = requests.get(f"{GATEWAY_URL}/balance/me", headers=headers)
    r.raise_for_status()
    return r.json()["balance"]

def test_deposit_updates_balance(auth_headers, idempotency_key):
    """
    Tests the deposit flow:
    1. Gets initial balance.
    2. Makes a deposit using an idempotency key.
    3. Gets final balance and verifies it increased correctly.
    """
    initial_balance = get_current_balance(auth_headers)
    deposit_amount = 150.75
    
    headers = {**auth_headers, "Idempotency-Key": idempotency_key}
    payload = {"amount": deposit_amount} # Gateway injects user_id
    
    try:
        r_deposit = requests.post(f"{GATEWAY_URL}/ledger/deposit", json=payload, headers=headers)
        r_deposit.raise_for_status()
        deposit_tx = r_deposit.json()
        
        assert deposit_tx["status"] == "COMPLETED", "Deposit status should be COMPLETED"
        assert deposit_tx["amount"] == deposit_amount, "Deposit amount mismatch"
        
        final_balance = get_current_balance(auth_headers)
        
        # --- THE CRITICAL CHECK ---
        expected_balance = initial_balance + deposit_amount
        assert final_balance == pytest.approx(expected_balance), \
               f"Balance incorrect after deposit. Expected {expected_balance}, got {final_balance}"
               
    except requests.exceptions.RequestException as e:
        pytest.fail(f"Deposit request failed: {e}")

def test_deposit_idempotency(auth_headers, idempotency_key):
    """
    Tests that sending the same deposit request twice with the same
    Idempotency-Key results in only one actual deposit.
    """
    initial_balance = get_current_balance(auth_headers)
    deposit_amount = 50.0
    
    headers = {**auth_headers, "Idempotency-Key": idempotency_key} # Use the SAME key
    payload = {"amount": deposit_amount}
    
    # --- First Deposit ---
    r1 = requests.post(f"{GATEWAY_URL}/ledger/deposit", json=payload, headers=headers)
    r1.raise_for_status()
    tx1_id = r1.json()["id"]
    
    # --- Second (Duplicate) Deposit ---
    r2 = requests.post(f"{GATEWAY_URL}/ledger/deposit", json=payload, headers=headers)
    r2.raise_for_status()
    tx2_id = r2.json()["id"]

    # --- Verification ---
    # The transaction ID returned should be the SAME
    assert tx1_id == tx2_id, "Duplicate deposit should return the original transaction ID"
    
    final_balance = get_current_balance(auth_headers)
    expected_balance = initial_balance + deposit_amount # Should only increase ONCE
    
    assert final_balance == pytest.approx(expected_balance), \
           f"Balance incorrect after duplicate deposit. Expected {expected_balance}, got {final_balance}"

def test_transfer_updates_balance(auth_headers, idempotency_key):
    """
    Tests the transfer flow (to mock Bank B):
    1. Deposits funds first.
    2. Gets initial balance.
    3. Makes a transfer.
    4. Gets final balance and verifies it decreased correctly.
    """
    # 1. Ensure sufficient funds by depositing first
    deposit_key = str(uuid.uuid4())
    deposit_headers = {**auth_headers, "Idempotency-Key": deposit_key}
    requests.post(f"{GATEWAY_URL}/ledger/deposit", json={"amount": 500.0}, headers=deposit_headers).raise_for_status()
    
    initial_balance = get_current_balance(auth_headers)
    transfer_amount = 120.25
    
    headers = {**auth_headers, "Idempotency-Key": idempotency_key}
    payload = {
        "amount": transfer_amount, 
        "to_bank": "BankB", 
        "to_account": "recipient@bankb.com"
    } # Gateway injects user_id
    
    try:
        r_transfer = requests.post(f"{GATEWAY_URL}/ledger/transfer", json=payload, headers=headers)
        r_transfer.raise_for_status()
        transfer_tx = r_transfer.json()
        
        assert transfer_tx["status"] == "COMPLETED", "Transfer status should be COMPLETED"
        assert transfer_tx["amount"] == transfer_amount, "Transfer amount mismatch"
        
        final_balance = get_current_balance(auth_headers)
        
        # --- THE CRITICAL CHECK ---
        expected_balance = initial_balance - transfer_amount
        assert final_balance == pytest.approx(expected_balance), \
               f"Balance incorrect after transfer. Expected {expected_balance}, got {final_balance}"
               
    except requests.exceptions.RequestException as e:
        pytest.fail(f"Transfer request failed: {e}")

def test_transfer_insufficient_funds(auth_headers, idempotency_key):
    """
    Tests that a transfer fails with a 400 error if funds are insufficient.
    """
    current_balance = get_current_balance(auth_headers)
    transfer_amount = current_balance + 100.0 # Amount guaranteed to be too high
    
    headers = {**auth_headers, "Idempotency-Key": idempotency_key}
    payload = {
        "amount": transfer_amount, 
        "to_bank": "BankB", 
        "to_account": "recipient@bankb.com"
    }
    
    r_transfer = requests.post(f"{GATEWAY_URL}/ledger/transfer", json=payload, headers=headers)
    
    # Expecting a 400 Bad Request (Insufficient Funds) from balance_service, proxied by gateway
    assert r_transfer.status_code == 400, f"Expected 400 for insufficient funds, got {r_transfer.status_code}"
    
    # Verify balance hasn't changed
    final_balance = get_current_balance(auth_headers)
    assert final_balance == pytest.approx(current_balance), "Balance should not change on failed transfer"