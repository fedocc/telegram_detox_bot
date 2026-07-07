from __future__ import annotations

from app.config import get_settings
from app.llm.client import HaikuClient


def main() -> None:
    client = HaikuClient(get_settings())
    result = client.classify_p0(
        {
            "message": {
                "chat_title": "Test",
                "sender_name": "Test",
                "text": "Please call me back in one hour.",
            },
            "context": [],
        }
    )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
