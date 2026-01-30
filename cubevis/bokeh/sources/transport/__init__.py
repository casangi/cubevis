from cubevis.utils import is_colab
import websockets
import http
from websockets.http11 import Response # Available in newer versions

log_path = "/content/package_debug.txt"

try:
    # Modern implementation (v14+)
    from websockets.server import ServerConnection as BaseConnection
    IS_LEGACY = False
except ImportError:
    # Legacy implementation (<v14)
    from websockets.server import WebSocketServerProtocol as BaseConnection
    IS_LEGACY = True

class ColabWebSocketServerProtocol(BaseConnection):
    def __init__(self, *args, **kwargs):
        with open(log_path, "a") as f:
            print(f"ColabWebSocketServerProtocol.__init__ Initializing Protocol with args={args} kwargs={kwargs}", file=f)
        super().__init__(*args, **kwargs)

    async def process_request(self, *args):
        """Handle both old (path, headers) and new (request) signatures."""
        if IS_LEGACY:
            path, request_headers = args
            request_method = getattr(self, "request_method", "GET")
        else:
            # Modern version passes (request) as the only arg
            request = args[0]
            path = request.path
            request_headers = request.headers
            request_method = request.method

        with open(log_path, "a") as f:
            print(f"ColabWebSocketServerProtocol.process_request: request_header={request_header} request_method={request_method}", file=f)

        # Logic for CORS/OPTIONS remains similar, but response format differs
        is_upgrade = "upgrade" in request_headers.get("Connection", "").lower()

        if not is_upgrade:
            origin = request_headers.get("Origin")
            headers = {"Content-Type": "text/plain", "Connection": "close"}
            if origin:
                headers.update({
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Credentials": "true",
                })

            if request_method == "OPTIONS":
                return self._format_response(http.HTTPStatus.NO_CONTENT, headers, b"")
            if request_method == "GET":
                return self._format_response(http.HTTPStatus.OK, headers, b"OK")

        return None

    def _format_response(self, status, headers, body):
        """Helper to return the correct type based on version."""
        if IS_LEGACY:
            # Legacy expects (status, headers, body)
            return status, headers, body
        else:
            # Modern expects a Response object or self.respond call
            return self.respond(status, body, headers)

def create_ws_server(callback, ip_address, port):
    """
    Uniform wrapper for creating a WebSocket server supporting all versions.
    """
    # Prepare base arguments
    kwargs = {
        "ws_handler": callback, # In newer versions 'callback' is the first positional or 'ws_handler'
        "host": "0.0.0.0" if is_colab() else ip_address,
        "port": port,
        "origins": None
    }

    # Inject the custom protocol class using the correct version-specific key
    if IS_LEGACY:
        kwargs["create_protocol"] = ColabWebSocketServerProtocol
    else:
        kwargs["create_connection"] = ColabWebSocketServerProtocol

    if is_colab():
        with open(log_path, "a") as f:
            f.write(f"Websocket startup: {kwargs['host']}:{port}\n")
            f.write(f"Using class: {ColabWebSocketServerProtocol.__name__}\n")
            f.write(f"Version mode: {'Legacy' if IS_LEGACY else 'Modern'}\n")

    # Use **kwargs to bypass signature differences between library versions
    return websockets.serve(**kwargs)
