import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import uvicorn
from src.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.SANDBOX_HUB_HOST,
        port=settings.SANDBOX_HUB_PORT,
        reload=False,
    )
