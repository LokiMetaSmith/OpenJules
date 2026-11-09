"""Integration tests for WebSocket real-time thought and diff streaming."""

import pytest
from fastapi.testclient import TestClient

from src.api.webhook import app
from src.api.ws import ws_manager


def test_websocket_stream_connection():
    client = TestClient(app)
    task_id = "test-ws-task-123"

    with client.websocket_connect(f"/ws/tasks/{task_id}") as websocket:
        # Initial greeting from server
        data = websocket.receive_json()
        assert data["type"] == "connected"
        assert data["task_id"] == task_id

        # Send test ping message from client
        websocket.send_text('{"ping": "hello agent"}')
        ack = websocket.receive_json()
        assert ack["type"] == "ack"
        assert ack["received"] == {"ping": "hello agent"}
