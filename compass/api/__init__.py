"""The ASGI front door.

Only `server.py` and the built web UI beside it. The application is the four
sections it mounts — compass.home, compass.code, compass.design and the shared
routes in compass.common — and this is deliberately the one path that does not
move, because `compass.api.server:app` is what uvicorn is pointed at.
"""
