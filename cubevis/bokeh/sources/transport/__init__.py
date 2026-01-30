from cubevis.utils import is_colab
import websockets
import http
import asyncio
try:
    from websockets.http11 import Response
except ImportError:
    Response = None # Fallback for very old versions

log_path = "/content/package_debug.txt"

try:
    from websockets.server import ServerConnection as BaseConnection
    IS_LEGACY = False
except ImportError:
    from websockets.server import WebSocketServerProtocol as BaseConnection
    IS_LEGACY = True

def universal_process_request(*args):
    """
    Handles CORS priming.
    Legacy signature: (path, request_headers)
    Modern signature: (request)
    """
    if IS_LEGACY:
        # Legacy passes: (path, headers)
        path, headers = args[0], args[1]
        method = "GET"
    else:
        request = args[0]
        path, headers, method = request.path, request.headers, request.method

    with open(log_path, "a") as f:
        f.write(f"Top-level process_request: {method} {path}\n")

    is_upgrade = "upgrade" in headers.get("Connection", "").lower()

    if not is_upgrade:
        origin = headers.get("Origin", "*")
        resp_headers = {
            "Content-Type": "text/plain",
            "Connection": "close",
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, OPTIONS, POST",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
        }

        if method == "OPTIONS":
            return (http.HTTPStatus.NO_CONTENT, resp_headers, b"") if IS_LEGACY else Response(http.HTTPStatus.NO_CONTENT, "No Content", resp_headers)

        if method == "GET":
            return (http.HTTPStatus.OK, resp_headers, b"OK") if IS_LEGACY else Response(http.HTTPStatus.OK, "OK", resp_headers)

    return None

class ColabWebSocketServerProtocol(BaseConnection):
    def __init__(self, *args, **kwargs):
        with open(log_path, "a") as f:
            f.write(f"ColabWebSocketServerProtocol.__init__ constructed\n")
        super().__init__(*args, **kwargs)

def create_ws_server(callback, ip_address, port):
    host = "0.0.0.0" if is_colab() else ip_address

    # Pass everything. websockets.serve ignores unknown kwargs.
    conf = {
        "host": host,
        "port": port,
        "origins": None,
        "process_request": universal_process_request,
        "create_connection": ColabWebSocketServerProtocol,
        "create_protocol": ColabWebSocketServerProtocol
    }

    if is_colab():
        with open(log_path, "a") as f:
            f.write(f"Websocket startup: {host}:{port} | Mode: {'Legacy' if IS_LEGACY else 'Modern'}\n")

    return websockets.serve(callback, **conf)
