"""WebSocket routing.

Consumers are registered here by the contexts that own them. Keeping the URL
table in `realtime` rather than scattered across apps means the ASGI entrypoint
has exactly one import, which matters because Channels routing is evaluated at
process start and a circular import there is an unbootable server.
"""

from django.urls import path

from realtime import consumers

websocket_urlpatterns = [
    # Live odds, ticket counters and the "puoi estrarre ora" prompt.
    path("ws/sale/<uuid:sale_id>/", consumers.SaleConsumer.as_asgi()),
    # Drives a single minigame round: spin, reels, settle.
    path("ws/game/<uuid:round_id>/", consumers.GameConsumer.as_asgi()),
    # Seller-vs-seller duels, both players on the same socket group.
    path("ws/duel/<uuid:duel_id>/", consumers.DuelConsumer.as_asgi()),
    # Per-user firehose: drips landing, credits arriving, duel challenges.
    path("ws/me/", consumers.PersonalConsumer.as_asgi()),
]
