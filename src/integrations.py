"""Thin MCP clients — official remote servers own the brittle SaaS logic.
We only map validated tasks onto tool calls."""
import logging
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from src.config import settings

log = logging.getLogger(__name__)


async def _call(url: str, tool: str, args: dict) -> Any:
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            return result.content


# ponytail: tool names below match the official Atlassian Rovo / Notion MCP servers as of 2026-02 GA;
# if a server renames a tool, list tools with session.list_tools() and update here — nothing else moves.

async def create_jira_issue(task: dict) -> Any:
    return await _call(settings.jira_mcp_url, "createJiraIssue", {
        "projectKey": settings.jira_project_key,
        "summary": task["title"],
        "issueTypeName": "Task",
        "description": task.get("detail", ""),
    })


async def create_notion_page(task: dict) -> Any:
    return await _call(settings.notion_mcp_url, "notion-create-pages", {
        "parent": {"data_source_id": settings.notion_data_source_id},
        "pages": [{"properties": {"title": task["title"]},
                   "content": task.get("detail", "")}],
    })
