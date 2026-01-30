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
    try:
        if IS_LEGACY:
            path, headers = args
            method = "GET"
        else:
            # IMPORTANT: args is (RequestObject,)
            request = args[0]
            path, headers, method = request.path, request.headers, request.method

        with open(log_path, "a") as f:
            f.write(f"RECEIVED: {method} {path} | Upgrade: {headers.get('Upgrade')}\n")

        # Handle CORS
        if "upgrade" not in headers.get("Connection", "").lower():
            origin = headers.get("Origin", "*")
            resp_headers = {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Methods": "GET, OPTIONS, POST",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            }
            if IS_LEGACY:
                return (http.HTTPStatus.OK, resp_headers, b"OK")
            return Response(http.HTTPStatus.OK, "OK", resp_headers)

    except Exception as e:
        with open(log_path, "a") as f:
            f.write(f"ERROR in process_request: {str(e)}\n")
    
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
