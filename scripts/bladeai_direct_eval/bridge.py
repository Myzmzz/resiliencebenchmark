"""容器无网络；通过共享 Unix socket 连接到本实例代理。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse
import uvicorn
import websockets

HOP = {'host', 'connection', 'keep-alive', 'transfer-encoding', 'upgrade', 'proxy-authorization', 'proxy-authenticate', 'content-length', 'authorization'}


async def forward(request: Request, client: httpx.AsyncClient, url: str):
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    try:
        response = await client.send(client.build_request(request.method, url, headers=headers, content=await request.body()), stream=True)
    except httpx.HTTPError as exc:
        return JSONResponse({'error': type(exc).__name__}, status_code=502)
    return StreamingResponse(response.aiter_raw(), status_code=response.status_code, headers={k: v for k, v in response.headers.items() if k.lower() not in HOP}, background=BackgroundTask(response.aclose))


async def relay_ws(socket, upstream):
    await socket.accept(subprotocol=upstream.subprotocol)

    async def send_up():
        while True:
            message = await socket.receive()
            if message['type'] == 'websocket.disconnect':
                break
            await upstream.send(message.get('bytes') if message.get('bytes') is not None else message['text'])

    async def send_down():
        async for message in upstream:
            await socket.send_bytes(message) if isinstance(message, bytes) else await socket.send_text(message)

    jobs = [asyncio.create_task(send_up()), asyncio.create_task(send_down())]
    _, pending = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
    for job in pending:
        job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    await socket.close()


async def main():
    outbound = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds='/sockets/gateway/api.sock'), timeout=httpx.Timeout(900, connect=10), trust_env=False)
    inbound = httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=10), trust_env=False)
    out_app, in_app = FastAPI(docs_url=None, openapi_url=None), FastAPI(docs_url=None, openapi_url=None)

    @out_app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])
    async def to_gateway(request: Request, path: str):
        return await forward(request, outbound, 'http://gateway/' + path + ('?' + request.url.query if request.url.query else ''))

    @out_app.websocket('/{path:path}')
    async def ws_gateway(socket: WebSocket, path: str):
        protocols = socket.scope.get('subprotocols', [])
        try:
            async with websockets.unix_connect('/sockets/gateway/api.sock', uri='ws://gateway/' + path + ('?' + socket.url.query if socket.url.query else ''), subprotocols=protocols) as upstream:
                await relay_ws(socket, upstream)
        except Exception:
            try:
                await socket.close(code=1008)
            except RuntimeError:
                pass

    @in_app.api_route('/{path:path}', methods=['GET', 'POST'])
    async def to_agent(request: Request, path: str):
        if path == 'api/v1/config' or path.endswith('/cancel'):
            return JSONResponse({'error': '本轮禁止此接口'}, status_code=403)
        return await forward(request, inbound, 'http://127.0.0.1:8089/' + path + ('?' + request.url.query if request.url.query else ''))

    socket = Path('/sockets/agent/bladeai.sock')
    socket.unlink(missing_ok=True)
    os.umask(0)
    servers = [uvicorn.Server(uvicorn.Config(out_app, host='127.0.0.1', port=18080, access_log=False, log_level='warning')), uvicorn.Server(uvicorn.Config(in_app, uds=str(socket), access_log=False, log_level='warning'))]
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == '__main__':
    asyncio.run(main())
