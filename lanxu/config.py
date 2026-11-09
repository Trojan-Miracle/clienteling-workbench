from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent

def load_env():
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                if key.startswith(('DEEPSEEK_', 'LANXU_')):
                    os.environ.setdefault(key, value.strip().strip('\"\''))

load_env()
DATA = ROOT / 'data'
ARTIFACTS = ROOT / 'artifacts'
