from cubevis.utils import is_colab
import websockets
import http
from websockets.http import Headers

log_path = "/content/package_debug.txt"

class ColabWebSocketServerProtocol(websockets.WebSocketServerProtocol):

    def __init__(self, *args, **kwargs):
        # Debug: Print or log the parameters
        with open(log_path, "a") as f:
            print(f"ColabWebSocketServerProtocol.__init__ Initializing Protocol with args={args} kwargs={kwargs}", file=f)
        super().__init__(*args, **kwargs)

    async def process_request(self, path, request_headers):
        with open(log_path, "a") as f:
            print("In ColabWebSocketServerProtocol.process_request...", file=f)

        is_upgrade = "upgrade" in request_headers.get("Connection", "").lower()

        # Get the request method
        request_method = self.request_method
        with open(log_path, "a") as f:
            print(f"Handling request method: {request_method}", file=f)

        if not is_upgrade:
            response_headers = Headers()
            origin = request_headers.get("Origin")

            if origin:
                response_headers["Access-Control-Allow-Origin"] = origin
                response_headers["Access-Control-Allow-Credentials"] = "true"
                # Add necessary headers for the OPTIONS preflight response
                response_headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
                response_headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"

            response_headers["Content-Type"] = "text/plain"
            response_headers["Connection"] = "close"

            # If it is an OPTIONS request, return 204 No Content immediately
            if request_method == "OPTIONS":
                return http.HTTPStatus.NO_CONTENT, response_headers, b"" # 204 Status and empty body

            # If it is a GET request (the priming fetch), return 200 OK
            if request_method == "GET":
                 return http.HTTPStatus.OK, response_headers, b"OK"

        # Proceed with standard WS handshake
        return None

def create_ws_server(callback, ip_address, port):
    """
    Uniform wrapper for creating a WebSocket server.
    """
    if is_colab( ):
        with open(log_path, "a") as f:
            print( f'''websocket startup: {ip_address}/{port} (bind IP 0.0.0.0)
with websockets.serve( callback, "0.0.0.0", {port}, origins=None, create_protocol={ColabWebSocketServerProtocol} )"''' , file=f)
        return websockets.serve( callback,
                                 "0.0.0.0",
                                 port,
                                 origins=None,
                                 create_protocol=ColabWebSocketServerProtocol # This bypasses the strict check
                                )
    else:
        return websockets.serve( callback, ip_address, port )
