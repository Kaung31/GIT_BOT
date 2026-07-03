"""Notion pages are created against a data_source_id, not the database_id from the URL.
This looks up the data source(s) for a database so you can fill NOTION_DATA_SOURCE_ID.

Usage: uv run python -m scripts.notion_data_source_id <database_id>
(database_id is the 32-char id in the database URL, dashes optional)
"""
import asyncio
import sys

import httpx

from src.config import settings


async def main(database_id: str) -> None:
    async with httpx.AsyncClient(headers={
        "Authorization": f"Bearer {settings.notion_api_token}",
        "Notion-Version": settings.notion_api_version,
    }) as client:
        resp = await client.get(f"https://api.notion.com/v1/databases/{database_id}")
        resp.raise_for_status()
        for ds in resp.json()["data_sources"]:
            print(f"{ds['name']}: {ds['id']}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
