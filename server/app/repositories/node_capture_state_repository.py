from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.node_capture_state import NodeCaptureState


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class NodeCaptureStateRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, node_id: str) -> NodeCaptureState | None:
        return self.db.get(NodeCaptureState, node_id)

    def record(self, node_id: str, values: dict[str, Any]) -> NodeCaptureState:
        state = self.get(node_id)

        if state is None:
            state = NodeCaptureState(node_id=node_id)
            self.db.add(state)

        for field_name, value in values.items():
            setattr(state, field_name, value)

        state.updated_at = utc_now()
        self.db.commit()
        self.db.refresh(state)

        return state

    def delete(self, node_id: str) -> bool:
        state = self.get(node_id)

        if state is None:
            return False

        self.db.delete(state)
        self.db.commit()

        return True
