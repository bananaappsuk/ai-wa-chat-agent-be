import asyncio
import json
from typing import Any
from fastapi import WebSocket


class WSManager:
    def __init__(self) -> None:
        self._conns: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._conns.setdefault(user_id, set()).add(ws)

    async def disconnect(self, user_id: str, ws: WebSocket) -> None:
        async with self._lock:
            conns = self._conns.get(user_id)
            if conns and ws in conns:
                conns.remove(ws)
                if not conns:
                    self._conns.pop(user_id, None)

    async def push(self, user_id: str, event: str, payload: Any) -> None:
        msg = json.dumps({"event": event, "data": payload})
        async with self._lock:
            conns = list(self._conns.get(user_id, ()))
        if not conns:
            return

        async def _send(ws: WebSocket):
            try:
                await ws.send_text(msg)
                return None
            except Exception:
                return ws

        results = await asyncio.gather(*(_send(ws) for ws in conns), return_exceptions=True)
        dead = [r for r in results if isinstance(r, WebSocket)]
        if dead:
            async with self._lock:
                conns_set = self._conns.get(user_id)
                if conns_set:
                    for ws in dead:
                        conns_set.discard(ws)
                    if not conns_set:
                        self._conns.pop(user_id, None)


ws_manager = WSManager()
