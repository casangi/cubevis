from cubevis.utils import is_colab
import websockets
import http
from websockets.server import serve
from websockets.http11 import Response

def colab_cors_handler(request):
    """ Handles the 'priming' fetch from TypeScript in websockets v15+ """
    # If the browser is just 'priming' (not upgrading to WS)
    if "upgrade" not in request.headers.get("Connection", "").lower():
        origin = request.headers.get("Origin", "*")
        headers = {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }
        # Return a standard HTTP 200 for the priming fetch
        return Response(http.HTTPStatus.OK, "OK", headers)
    return None # Continue to WebSocket handshake if it IS an upgrade

def create_ws_server(callback, ip_address, port):
    if is_colab():
        print(f"Websocket startup: 0.0.0.0:{port} (CORS Enabled)")
        return serve(
            callback,
            "0.0.0.0",
            port,
            process_request=colab_cors_handler # Add this to fix the fetch error
        )
    else:
        return serve(callback, ip_address, port)
