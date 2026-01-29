import websockets
from cubevis.utils import is_colab
from websockets.http import Headers
import http

class ColabWebSocketServerProtocol(websockets.WebSocketServerProtocol):
    async def process_request(self, path, request_headers):
        is_upgrade = "upgrade" in request_headers.get("Connection", "").lower()

        if not is_upgrade:
            # 1. Prepare CORS headers so the 'fetch' succeeds
            response_headers = Headers()
            # We must echo the specific origin for credentials to work
            origin = request_headers.get("Origin", "*")
            response_headers["Access-Control-Allow-Origin"] = origin
            response_headers["Access-Control-Allow-Credentials"] = "true"
            response_headers["Content-Type"] = "text/plain"

            return http.HTTPStatus.OK, response_headers, b"OK"

        return None

def create_ws_server(callback, ip_address, port):
    """
    Uniform wrapper for creating a WebSocket server.
    """
    if is_colab( ):
        print( f'''websocket startup: {ip_address}/{port} (bind IP 0.0.0.0)
with websockets.serve( callback, "0.0.0.0", {port}, origins=None, create_protocol={ColabWebSocketServerProtocol} )"''' )
        return websockets.serve( callback,
                                 "0.0.0.0",
                                 port,
                                 origins=None,
                                 create_protocol=ColabWebSocketServerProtocol # This bypasses the strict check
                                )
    else:
        return websockets.serve( callback, ip_address, port )
