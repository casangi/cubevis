import websockets
from cubevis.utils import is_colab

class ColabWebSocketServerProtocol(websockets.WebSocketServerProtocol):
    async def process_request(self, path, request_headers):
        # Force the connection to be accepted even if the proxy stripped headers
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
