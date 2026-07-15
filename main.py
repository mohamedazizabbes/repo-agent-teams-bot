import asyncio
import json
import os
import re
import uuid
from pathlib import Path

from dotenv import load_dotenv
from aiohttp import web
from botbuilder.integration.aiohttp import CloudAdapter, ConfigurationBotFrameworkAuthentication
from botbuilder.core import MessageFactory, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ConversationReference

from rag_client import ask_rag, ingest_repo, ingest_status

load_dotenv()

_required = ["MICROSOFT_APP_ID", "MICROSOFT_APP_PASSWORD", "RAG_BACKEND_URL"]
_missing = [v for v in _required if not os.environ.get(v)]
if _missing:
    print(f"ERROR: Missing env vars: {', '.join(_missing)}")
    print("Set them in Render → Settings → Environment and redeploy.")
    raise SystemExit(1)

config = ConfigurationBotFrameworkAuthentication({
    "MicrosoftAppType": "MultiTenant",
    "MicrosoftAppId": os.environ["MICROSOFT_APP_ID"],
    "MicrosoftAppPassword": os.environ["MICROSOFT_APP_PASSWORD"],
})
ADAPTER = CloudAdapter(config)

# ---------------------------------------------------------------------------
# Markdown post-processor
# ---------------------------------------------------------------------------

def format_message(text: str) -> str:
    """Convert fenced code blocks to indented code for Teams plain-text messages."""
    parts = re.split(r'(```[\w]*\n.*?```)', text, flags=re.DOTALL)
    result = []
    for part in parts:
        if part.startswith("```"):
            inner = re.match(r'```[\w]*\n(.*?)```', part, re.DOTALL)
            if inner:
                code = inner.group(1)
                lines = code.split("\n")
                if lines and re.match(r'^[a-z]+\s*$', lines[0].strip()):
                    lines = lines[1:]
                while lines and not lines[-1].strip():
                    lines.pop()
                result.append("\n".join("    " + line for line in lines))
            else:
                result.append(part)
        else:
            result.append(part)
    return "".join(result)


# ---------------------------------------------------------------------------
# Repo management (identical to slack-bot)
# ---------------------------------------------------------------------------

REPOS_FILE = Path(__file__).parent / "repos.json"


def _load_repos() -> dict[str, str]:
    with open(REPOS_FILE) as f:
        return json.load(f)


def _find_repo(alias: str) -> tuple[str, str] | None:
    repos = _load_repos()
    alias_lower = alias.lower()
    for key, url in repos.items():
        if key.lower() == alias_lower:
            return key, url
    return None


def _normalize(name: str) -> str:
    return name.lower().removesuffix(".git")


def repo_name_from_url(url: str) -> str:
    name = Path(url.rstrip("/")).name
    if name.endswith(".git"):
        name = name[:-4]
    return name


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_conversation_repos: dict[str, dict] = {}
_in_flight: set[str] = set()


# ---------------------------------------------------------------------------
# Proactive messaging helper
# ---------------------------------------------------------------------------

async def _send(conversation_ref: ConversationReference, text: str) -> None:
    text = format_message(text)

    async def _callback(context: TurnContext) -> None:
        await context.send_activity(MessageFactory.text(text))

    await ADAPTER.continue_conversation(None, conversation_ref, _callback)


# ---------------------------------------------------------------------------
# Bot logic
# ---------------------------------------------------------------------------

async def on_message(context: TurnContext) -> None:
    activity = context.activity
    if activity.type != ActivityTypes.message:
        return

    text = (activity.text or "").strip()

    # Strip <at>BotName</at> mention tags (Teams format)
    text = re.sub(r"<at>[^<]*</at>", "", text).strip()

    conversation_id = activity.conversation.id
    m = re.search(r"/(\S+)", text)

    if m:
        raw_repo = m.group(1)
        alias = _normalize(raw_repo)

        found = _find_repo(alias)
        if not found:
            repos = _load_repos()
            known = ", ".join(f"/{k}" for k in repos)
            await context.send_activity(
                MessageFactory.text(f"Unknown repo /{raw_repo}. Known: {known}")
            )
            return

        _, repo_url = found
        qdrant_name = repo_name_from_url(repo_url)
        question = re.sub(r"/\S+", "", text, count=1).strip()

        _conversation_repos[conversation_id] = {
            "repo_name": qdrant_name,
            "repo_url": repo_url,
        }
    else:
        if conversation_id in _conversation_repos:
            qdrant_name = _conversation_repos[conversation_id]["repo_name"]
            repo_url = _conversation_repos[conversation_id]["repo_url"]
            question = text
        else:
            repos = _load_repos()
            known = ", ".join(f"/{k}" for k in repos)
            await context.send_activity(
                MessageFactory.text(
                    f"Usage: /repo_name your question\nKnown repos: {known}"
                )
            )
            return

    event_id = activity.id or f"{conversation_id}:{uuid.uuid4().hex}"
    if event_id in _in_flight:
        return
    _in_flight.add(event_id)

    conversation_ref = TurnContext.get_conversation_reference(activity)
    session_id = f"teams:{conversation_id}:{activity.id or uuid.uuid4().hex}"

    async def _safe_answer() -> None:
        try:
            await _answer(
                conversation_ref, question, qdrant_name, repo_url, session_id
            )
        finally:
            _in_flight.discard(event_id)

    asyncio.create_task(_safe_answer())


async def _answer(
    conversation_ref: ConversationReference,
    question: str,
    target_repo: str,
    repo_url: str,
    session_id: str,
) -> None:
    # Check / trigger indexing
    try:
        status = await ingest_status(target_repo)
    except Exception:
        status = "unknown"

    if status == "ready":
        pass
    elif status == "indexing":
        await _send(
            conversation_ref,
            f"Indexing `{target_repo}` is already in progress — waiting...",
        )
        for _ in range(100):  # 100 x 3s = 5 min max
            await asyncio.sleep(3)
            try:
                s = await ingest_status(target_repo)
            except Exception:
                s = "unknown"
            if s == "ready":
                break
            elif s.startswith("error"):
                await _send(conversation_ref, f"Indexing failed: {s}")
                return
        else:
            await _send(
                conversation_ref,
                "Indexing is taking longer than expected. Please try again in a minute.",
            )
            return
    else:
        await _send(
            conversation_ref,
            f"Indexing `{target_repo}` for the first time — this may take a moment...",
        )
        try:
            result = await ingest_repo(repo_url)
        except Exception:
            result = "error"

        if result == "indexing":
            for _ in range(100):
                await asyncio.sleep(3)
                try:
                    s = await ingest_status(target_repo)
                except Exception:
                    s = "unknown"
                if s == "ready":
                    break
                elif s.startswith("error"):
                    await _send(conversation_ref, f"Indexing failed: {s}")
                    return
            else:
                await _send(
                    conversation_ref,
                    "Indexing is taking longer than expected. Please try again in a minute.",
                )
                return

    answer = await ask_rag(question, target_repo, session_id)
    if not answer or not answer.strip():
        answer = (
            "Sorry, I couldn't generate an answer. "
            "Try rephrasing more concisely."
        )
    await _send(conversation_ref, answer)


# ---------------------------------------------------------------------------
# Webhook endpoints
# ---------------------------------------------------------------------------

async def messages(request: web.Request) -> web.Response:
    body = await request.json()
    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")
    await ADAPTER.process_activity(activity, auth_header, on_message)
    return web.Response(status=202)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


app = web.Application()
app.router.add_post("/api/messages", messages)
app.router.add_get("/", health)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", 3978)))
