"""Authentication boundary shared by every API route."""

import secrets

from starlette.responses import JSONResponse


def print_session_token(token: str, origin: str) -> None:
    """Write the secret to the controlling local terminal, never app logs."""
    try:
        with open('/dev/tty', 'w', encoding='utf-8') as terminal:
            terminal.write(f'FileMindDemo: {origin}\nSession token: {token}\n')
            terminal.flush()
    except OSError:
        raise RuntimeError('Start FileMindDemo in a local interactive terminal to receive the session token') from None


class LocalSessionMiddleware:
    def __init__(self, app, session, origin: str):
        self.app = app
        self.session = session
        self.origin = origin
        self.host = origin.removeprefix('http://')

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        if scope['type'] == 'websocket':
            return await send({'type': 'websocket.close', 'code': 1008})
        headers = {}
        for key, value in scope['headers']:
            headers.setdefault(key.lower(), []).append(value)
        hosts = headers.get(b'host', [])
        origins = headers.get(b'origin', [])
        if hosts != [self.host.encode('ascii')]:
            response = JSONResponse({'detail': 'Invalid Host'}, status_code=400)
        elif len(origins) > 1 or (origins and origins[0] and origins[0] != self.origin.encode('ascii')):
            response = JSONResponse({'detail': 'Invalid Origin'}, status_code=403)
        elif scope['path'] == '/api' or scope['path'].startswith('/api/'):
            authorization = headers.get(b'authorization', [])
            parts = authorization[0].split() if len(authorization) == 1 else []
            token = self.session.get('token')
            if (len(parts) != 2 or parts[0].lower() != b'bearer' or token is None
                    or not secrets.compare_digest(parts[1], token.encode('ascii'))):
                response = JSONResponse({'detail': 'Invalid session token'}, status_code=401,
                                        headers={'WWW-Authenticate': 'Bearer'})
            else:
                response = None
        else:
            response = None

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] = list(message.get('headers', [])) + [
                    (b'cache-control', b'no-store'),
                    (b'referrer-policy', b'no-referrer'),
                    (b'x-content-type-options', b'nosniff'),
                    # Element Plus uses inline styles for component layout;
                    # scripts still require same-origin, precompiled assets.
                    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
                ]
            await send(message)

        if response is not None:
            return await response(scope, receive, secure_send)
        await self.app(scope, receive, secure_send)
