"""Human side: verdict cards with inline Approve/Reject buttons via the Telegram Bot API.
Long-polling, so Telegram needs no public URL (only the GitHub webhook does).
Only TELEGRAM_CHAT_ID may approve — a stranger finding the bot can't merge your code."""
import asyncio
import logging

import httpx

from src.config import settings

log = logging.getLogger(__name__)
API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"

# ponytail: no "Ask for changes" button — Approve/Reject covers the gate, and the revise
# loop already runs before the verdict; add a force_reply comment flow if you miss it


async def _post(method: str, **payload) -> dict:
    async with httpx.AsyncClient(timeout=70) as c:
        r = await c.post(f"{API}/{method}", json=payload)
        r.raise_for_status()
        return r.json()["result"]


async def send_text(text: str) -> None:
    await _post("sendMessage", chat_id=settings.telegram_chat_id, text=text[:4000])


async def send_approval(thread_id: str, card: str) -> None:
    await _post("sendMessage", chat_id=settings.telegram_chat_id, text=card[:4000],
                reply_markup={"inline_keyboard": [[
                    {"text": "✅ Approve", "callback_data": f"approve:{thread_id}"},
                    {"text": "❌ Reject", "callback_data": f"reject:{thread_id}"},
                ]]})


def verdict_card(state: dict) -> str:
    v = state.get("verdict") or {}
    findings = state.get("findings", [])
    top = "\n".join(f"• [{f.get('severity', '?')}] {f.get('title', '')}" for f in findings[:5])
    test = state.get("test_result") or {}
    test_line = "✅ tests pass" if test.get("passed") else f"❌ tests: {test.get('log', 'not run')[-200:]}"
    diff = (state.get("patch") or "")[:1500]
    return (f"🤖 Swarm verdict: {v.get('decision', '?')} (confidence {v.get('confidence', '?')})\n"
            f"{v.get('reasoning', '')[:400]}\n\n{test_line}\n\n"
            f"Findings ({len(findings)}):\n{top or '• none'}\n\n"
            f"```\n{diff}\n```")


async def poll_updates(on_decision) -> None:
    """Long-poll for button taps; on_decision(decision, thread_id) resumes the graph."""
    offset = 0
    log.info("telegram polling started")
    while True:
        try:
            updates = await _post("getUpdates", offset=offset, timeout=60,
                                  allowed_updates=["callback_query"])
        except Exception:
            log.exception("getUpdates failed, retrying")
            await asyncio.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            cq = u.get("callback_query")
            if not cq:
                continue
            if str(cq["message"]["chat"]["id"]) != str(settings.telegram_chat_id):
                log.warning("callback from unauthorized chat %s ignored", cq["message"]["chat"]["id"])
                continue
            decision, thread_id = cq["data"].split(":", 1)
            await _post("answerCallbackQuery", callback_query_id=cq["id"], text=f"{decision}d")
            try:
                await on_decision(decision, thread_id)
            except Exception:
                log.exception("resume failed for %s", thread_id)
                await send_text(f"⚠️ resume failed for run {thread_id} — check logs")
