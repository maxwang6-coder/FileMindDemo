"""所有 API 路由共用的本机会话鉴权边界。"""

import secrets

from starlette.responses import JSONResponse


# 仅向本机交互终端输出会话凭证，无终端时拒绝启动。
def print_session_token(token: str, origin: str) -> None:
    try:
        with open('/dev/tty', 'w', encoding='utf-8') as terminal:
            terminal.write(f'FileMindDemo: {origin}\nSession token: {token}\n')
            terminal.flush()
    except OSError:
        raise RuntimeError('Start FileMindDemo in a local interactive terminal to receive the session token') from None


class LocalSessionMiddleware:
    # 保存后续应用、会话容器和允许的同源地址，供请求校验使用。
    def __init__(self, app, session, origin: str):
        self.app = app
        self.session = session
        self.origin = origin
        self.host = origin.removeprefix('http://')

    # 逐次校验 Host、Origin 和 API 会话凭证，再拒绝请求或交给后续应用。
    async def __call__(self, scope, receive, send):
        # 1. 非 HTTP 生命周期消息直接转交；当前不提供 WebSocket，收到连接即关闭。
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        if scope['type'] == 'websocket':
            return await send({'type': 'websocket.close', 'code': 1008})
        # 2. 收集原始请求头并保留重复值，防止重复安全头绕过检查。
        headers = {}
        for key, value in scope['headers']:
            headers.setdefault(key.lower(), []).append(value)
        hosts = headers.get(b'host', [])
        origins = headers.get(b'origin', [])
        # 3. 先校验 Host 与有值 Origin；API 再校验 Bearer，失败时构造拒绝响应。
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

        # 给响应补充禁止缓存和浏览器安全头，再转发响应消息。
        async def secure_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] = list(message.get('headers', [])) + [
                    (b'cache-control', b'no-store'),
                    (b'referrer-policy', b'no-referrer'),
                    (b'x-content-type-options', b'nosniff'),
                    # 允许 Element Plus 布局所需的内联样式，脚本仍仅允许同源构建产物。
                    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
                ]
            await send(message)

        # 4. 拒绝响应直接发送；通过校验则调用后续应用，两条路径都通过安全响应包装器。
        if response is not None:
            return await response(scope, receive, secure_send)
        await self.app(scope, receive, secure_send)
