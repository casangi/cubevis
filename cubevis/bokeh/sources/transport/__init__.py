import websockets
from cubevis.utils import is_colab

class ColabWebSocketServerProtocol(websockets.WebSocketServerProtocol):
    async def process_request(self, path, request_headers):
        """
        Intervene before the handshake logic.
        If the proxy stripped 'Upgrade', we manually tell the server
        it's okay to proceed.
        """
        # If we see a standard GET (like our priming fetch),
        # return a 200 OK so the fetch doesn't 500.
        if "upgrade" not in request_headers.get("Connection", "").lower():
            return http.HTTPStatus.OK, [], b"OK"

        # For actual WS handshakes, return None to proceed to standard WS logic
        return None

def create_ws_server(callback, ip_address, port):
    """
    Uniform wrapper for creating a WebSocket server.
    """
    if is_colab( ):
        print( f"websocket startup: {ip_address}/{port} (bind IP 0.0.0.0)" )
        return websockets.serve( callback,
                                 "0.0.0.0",
                                 port,
                                 origins=None,
                                 create_protocol=ColabWebSocketServerProtocol # This bypasses the strict check
                                )
    else:
        return websockets.serve( callback, ip_address, port )
