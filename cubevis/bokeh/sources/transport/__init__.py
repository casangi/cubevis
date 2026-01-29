import websockets
from cubevis.utils import is_colab

def create_ws_server(callback, ip_address, port):
    """
    Uniform wrapper for creating a WebSocket server.
    """
    if is_colab( ):
        print( f"websocket startup: {ip_address}/{port}" )
        return websockets.serve( callback, ip_address, port, origins=None )
    else:
        return websockets.serve( callback, ip_address, port )
