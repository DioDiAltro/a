"""WebSocket consumers.

Contract for every consumer here: **the socket never decides anything.** It
relays. A client message asking to spin a reel calls the games service, which
settles the round in a database transaction and publishes an event; the socket's
job is to carry the result back and animate it.

That separation is what makes the game surfaces safe to make flashy — a client
that lies, replays or disconnects mid-animation cannot change an outcome that
was already committed server-side.
"""

from __future__ import annotations

import json

from channels.generic.websocket import AsyncJsonWebsocketConsumer


class _AuthedConsumer(AsyncJsonWebsocketConsumer):
    group_name: str = ""

    async def connect(self):
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4401)
            return
        self.user = user
        if self.group_name:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def broadcast(self, event):
        """Group handler. Payload is already shaped by the publishing context."""
        await self.send_json(event["payload"])

    @classmethod
    async def encode_json(cls, content):
        return json.dumps(content, default=str)


class SaleConsumer(_AuthedConsumer):
    async def connect(self):
        self.group_name = f"sale.{self.scope['url_route']['kwargs']['sale_id']}"
        await super().connect()


class GameConsumer(_AuthedConsumer):
    async def connect(self):
        self.group_name = f"game.{self.scope['url_route']['kwargs']['round_id']}"
        await super().connect()


class DuelConsumer(_AuthedConsumer):
    async def connect(self):
        self.group_name = f"duel.{self.scope['url_route']['kwargs']['duel_id']}"
        await super().connect()


class PersonalConsumer(_AuthedConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if user is not None and user.is_authenticated:
            self.group_name = f"user.{user.pk}"
        await super().connect()
