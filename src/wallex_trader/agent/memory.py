from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from wallex_trader.config import Settings
from wallex_trader.storage.repositories import Repository


class AgentMemory:
    """Persistent trade journal and compact lessons injected into prompts.

    Lessons are advisory. They never mutate hard risk limits or code.
    """

    def __init__(self, settings: Settings, repo: Repository) -> None:
        self.settings = settings
        self.repo = repo

    async def recent_for_prompt(self, symbol: str | None, limit: int = 6) -> list[dict[str, Any]]:
        return await self.repo.get_agent_notes(symbol=symbol, limit=limit)

    async def add_note(
        self,
        *,
        note: str,
        lesson: str = "",
        symbol: str | None = None,
        kind: str = "trade",
        outcome: str = "neutral",
        tags: list[str] | None = None,
        importance: float = 0.5,
        pnl_tmn: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.repo.add_agent_note(
            note=note,
            lesson=lesson,
            symbol=symbol,
            kind=kind,
            outcome=outcome,
            tags=tags,
            importance=importance,
            pnl_tmn=pnl_tmn,
            metadata=metadata,
        )
        await self._append_journal(
            {
                "ts": datetime.now(UTC).isoformat(),
                "kind": kind,
                "symbol": symbol,
                "outcome": outcome,
                "importance": importance,
                "pnl_tmn": pnl_tmn,
                "note": note,
                "lesson": lesson,
                "tags": tags or [],
                "metadata": metadata or {},
            }
        )

    async def _append_journal(self, entry: dict[str, Any]) -> None:
        path = self.settings.journal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        block = (
            f"\n## {entry['ts']} — {entry.get('kind', 'note')} "
            f"{('· ' + str(entry.get('symbol'))) if entry.get('symbol') else ''}\n\n"
            f"- Outcome: **{entry.get('outcome')}**\n"
            f"- Importance: {entry.get('importance')}\n"
            f"- PnL (TMN): {entry.get('pnl_tmn')}\n"
            f"- Tags: {', '.join(entry.get('tags') or []) or 'none'}\n\n"
            f"{entry.get('note')}\n\n"
            f"**Lesson:** {entry.get('lesson') or 'none'}\n"
        )
        if not path.exists():
            block = "# ARGUS Agent Journal\n\nAppend-only learning notes from shadow/live trading.\n" + block
        await asyncio.to_thread(_append_text, path, block)

    async def playbook_text(self, symbol: str | None = None, limit: int = 8) -> str:
        notes = await self.repo.get_agent_notes(symbol=symbol, limit=limit)
        if not notes:
            return "No prior lessons yet. Be conservative and collect evidence."
        lines = []
        for note in notes:
            tag_text = ",".join(note.get("tags") or [])
            lines.append(
                f"- [{note.get('outcome')}/{note.get('importance')}] "
                f"{note.get('lesson') or note.get('note')} ({tag_text})"
            )
        return "\n".join(lines)


def _append_text(path: Any, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
