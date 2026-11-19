"""Bound HTTP JSON memory use independently of a reverse proxy."""
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    def __init__(self, app, maximum):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope,receive,send)
        headers = dict(scope.get("headers",[]))
        try:
            declared = int(headers.get(b"content-length",b"0"))
        except ValueError:
            return await JSONResponse({"detail":"Content-Length inválido"},status_code=400)(scope,receive,send)
        if declared > self.maximum:
            return await JSONResponse({"detail":"Corpo da requisição excede o limite"},status_code=413)(scope,receive,send)
        total = 0
        async def bounded_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body",b""))
                if total > self.maximum:
                    raise HTTPException(413,"Corpo da requisição excede o limite")
            return message
        return await self.app(scope,bounded_receive,send)
