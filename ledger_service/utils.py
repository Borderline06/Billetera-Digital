# ledger_service/utils.py
from dotenv import load_dotenv
import os
import logging

def load_env_vars():
    """Carga variables de entorno y verifica que las esenciales existan."""
    load_dotenv()
    
    required_vars = ["BALANCE_SERVICE_URL", "MOCK_BANKB_URL", "CASSANDRA_HOST"]
    missing = [var for var in required_vars if not os.getenv(var)]
    
    if missing:
        msg = f"Variables de entorno faltantes: {', '.join(missing)}"
        logging.critical(msg)
        raise EnvironmentError(msg)