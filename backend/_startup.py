import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Resolve project root (afc_inventory-api/)
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 1. Add project root to PYTHONPATH so `import database` works
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 2. Load .env from root (only if not already loaded)
ENV_PATH = PROJECT_ROOT / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    print(f"⚠️ WARNING: No .env file found at {ENV_PATH}")
