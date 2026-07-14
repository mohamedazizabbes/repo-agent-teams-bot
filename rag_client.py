import os
import httpx

RAG_BASE = os.environ["RAG_BACKEND_URL"].rstrip("/")


async def ingest_repo(repo_url: str) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{RAG_BASE}/ingest",
            json={
                "repo_url": repo_url,
                "github_token": os.getenv("GITHUB_TOKEN"),
            },
        )
        try:
            data = resp.json()
            return data.get("status", "error")
        except Exception:
            return f"http_{resp.status_code}"


async def ingest_status(repo_name: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{RAG_BASE}/ingest/status/{repo_name}")
        try:
            data = resp.json()
            return data.get("status", "unknown")
        except Exception:
            return f"http_{resp.status_code}"


async def ask_rag(question: str, target_repo: str, session_id: str) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{RAG_BASE}/query",
            json={
                "question": question,
                "target_repo": target_repo,
                "session_id": session_id,
            },
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                return f"Error {resp.status_code}: {body[:200].decode(errors='replace')}"
            parts: list[str] = []
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            return "".join(parts)
                        parts.append(data)
    return "".join(parts)
