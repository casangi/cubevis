from cubevis.utils import is_colab
import websockets
import http
from websockets.server import serve
from websockets.http11 import Response

log_path = "/content/package_debug.txt"

def colab_cors_handler(request):
    """Answers the priming fetch once Google Proxy lets it through."""
    # LOG EVERY ATTEMPT - If this doesn't show up, traffic isn't reaching Python
    with open(log_path, "a") as f:
        f.write(f"V15 HANDLER: {request.method} to {request.path}\n")
    if "upgrade" not in request.headers.get("Connection", "").lower():
        origin = request.headers.get("Origin", "*")
        headers = {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
        }
        return Response(http.HTTPStatus.OK, "OK", headers)
    return None

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
