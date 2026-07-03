"""Jira/Notion writes — direct REST calls for now (API token, ~10min setup).

# ponytail: upgrade path — swap these two functions for MCP tool calls (Atlassian Rovo MCP,
# Notion's official hosted MCP) when OAuth app registration is worth it. Nothing else in the
# graph changes: mcp_integrator in graph.py only calls create_jira_issue/create_notion_page.
"""
import logging
from typing import Any

import httpx

from src.config import settings

log = logging.getLogger(__name__)


def _adf(text: str) -> dict:
    """Jira Cloud v3 wants rich text as Atlassian Document Format, not a plain string."""
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text or "(no detail)"}]}]}


async def create_jira_issue(task: dict) -> Any:
    async with httpx.AsyncClient(auth=(settings.jira_email, settings.jira_api_token)) as client:
        resp = await client.post(
            f"{settings.jira_base_url}/rest/api/3/issue",
            json={"fields": {
                "project": {"key": settings.jira_project_key},
                "summary": task["title"],
                "issuetype": {"name": "Task"},
                "description": _adf(task.get("detail", "")),
            }},
        )
        resp.raise_for_status()
        return resp.json()


async def create_notion_page(task: dict) -> Any:
    async with httpx.AsyncClient(headers={
        "Authorization": f"Bearer {settings.notion_api_token}",
        "Notion-Version": settings.notion_api_version,
    }) as client:
        resp = await client.post(
            "https://api.notion.com/v1/pages",
            json={
                "parent": {"data_source_id": settings.notion_data_source_id},
                "properties": {settings.notion_title_property: {
                    "title": [{"text": {"content": task["title"]}}]}},
                "children": [{"object": "block", "type": "paragraph",
                             "paragraph": {"rich_text": [{"text": {"content": task.get("detail", "")}}]}}],
            },
        )
        resp.raise_for_status()
        return resp.json()
