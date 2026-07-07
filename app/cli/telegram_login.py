from __future__ import annotations

import asyncio

from app.config import get_settings
from app.telegram.client import interactive_login

if __name__ == "__main__":
    asyncio.run(interactive_login(get_settings()))

