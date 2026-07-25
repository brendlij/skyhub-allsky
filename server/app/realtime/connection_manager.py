from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ManagedNode:
    node_id: str
    websocket: WebSocket | None = None
    online: bool = False
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_message_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ConnectionManager:
    def __init__(self):
        self._nodes: dict[str, ManagedNode] = {}
        self._dashboards: set[WebSocket] = set()

    async def connect(self, node_id: str, websocket: WebSocket):
        await websocket.accept()

        node = self._nodes.get(node_id)

        if node is None:
            node = ManagedNode(node_id=node_id)
            self._nodes[node_id] = node

        now = utc_now()

        node.websocket = websocket
        node.online = True
        node.connected_at = now
        node.disconnected_at = None
        node.last_seen_at = now
        node.last_message_type = "node.connected"

    def disconnect(self, node_id: str):
        node = self._nodes.get(node_id)

        if node is None:
            return

        node.websocket = None
        node.online = False
        node.disconnected_at = utc_now()
        node.last_message_type = "node.disconnected"

    def update_last_seen(
        self,
        node_id: str,
        message_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        node = self._nodes.get(node_id)

        if node is None:
            node = ManagedNode(node_id=node_id)
            self._nodes[node_id] = node

        node.last_seen_at = utc_now()

        if message_type:
            node.last_message_type = message_type

        if metadata:
            node.metadata.update(metadata)

    def get_node(self, node_id: str) -> ManagedNode | None:
        return self._nodes.get(node_id)

    def list_nodes(self) -> list[dict[str, Any]]:
        return [
            {
                "node_id": node.node_id,
                "online": node.online,
                "connected_at": node.connected_at.isoformat() if node.connected_at else None,
                "disconnected_at": node.disconnected_at.isoformat() if node.disconnected_at else None,
                "last_seen_at": node.last_seen_at.isoformat() if node.last_seen_at else None,
                "last_message_type": node.last_message_type,
                "metadata": node.metadata,
            }
            for node in self._nodes.values()
        ]
    
    def online_node_ids(self) -> list[str]:
        return [node.node_id for node in self._nodes.values() if node.online and node.websocket]

    async def send_to_node(self, node_id: str, message: dict) -> bool:
        node = self._nodes.get(node_id)

        if node is None or not node.online or node.websocket is None:
            return False

        await node.websocket.send_json(message)
        return True

    async def connect_dashboard(self, websocket: WebSocket):
        await websocket.accept()
        self._dashboards.add(websocket)

    def disconnect_dashboard(self, websocket: WebSocket):
        self._dashboards.discard(websocket)

    async def broadcast_dashboard(self, message: dict):
        disconnected = []

        for websocket in self._dashboards:
            try:
                await websocket.send_json(message)
            except Exception:
                disconnected.append(websocket)

        for websocket in disconnected:
            self.disconnect_dashboard(websocket)
