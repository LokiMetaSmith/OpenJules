"""WebSocket manager and endpoints for real-time thought and diff streaming."""

import asyncio
import json
import logging
from typing import Dict, List, Set
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

ws_router = APIRouter()


class ConnectionManager:
    """Manages active WebSocket connections per task."""

    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, task_id: str, websocket: WebSocket):
        await websocket.accept()
        if task_id not in self.active_connections:
            self.active_connections[task_id] = set()
        self.active_connections[task_id].add(websocket)

    def disconnect(self, task_id: str, websocket: WebSocket):
        if task_id in self.active_connections:
            self.active_connections[task_id].discard(websocket)
            if not self.active_connections[task_id]:
                del self.active_connections[task_id]

    async def broadcast(self, task_id: str, message: dict):
        if task_id in self.active_connections:
            dead_connections = set()
            for connection in self.active_connections[task_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    dead_connections.add(connection)
            for dead in dead_connections:
                self.active_connections[task_id].discard(dead)


ws_manager = ConnectionManager()


@ws_router.websocket("/ws/tasks/{task_id}")
async def websocket_task_stream(websocket: WebSocket, task_id: str):
    """Streams live agent thoughts, status, and working diffs to connected UI clients."""
    await ws_manager.connect(task_id, websocket)
    try:
        # Send initial confirmation
        await websocket.send_json({
            "type": "connected",
            "task_id": task_id,
            "message": f"Connected to live stream for task {task_id}",
        })
        while True:
            # Keep connection alive and accept incoming operator commands
            data = await websocket.receive_text()
            try:
                cmd_data = json.loads(data)
                # Echo or handle incoming client ping/chat
                await websocket.send_json({"type": "ack", "received": cmd_data})
            except Exception:
                await websocket.send_json({"type": "ack", "raw": data})
    except WebSocketDisconnect:
        ws_manager.disconnect(task_id, websocket)
    except Exception as e:
        logger.debug(f"WebSocket closed: {e}")
        ws_manager.disconnect(task_id, websocket)
