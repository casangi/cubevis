# iclean DataPipe Transport System

Complete implementation of pluggable transport system for iclean DataPipe, enabling:
- Multiplexed WebSocket connections
- Jupyter kernel comm integration
- Remote kernel support with reconnection
- Backward compatibility with existing code

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     Frontend (Browser)                       │
│  ┌─────────────────────────────────────────────────────┐   │
│  │         Bokeh UI (TypeScript DataPipe)              │   │
│  └────────────┬────────────────────────────────────────┘   │
└───────────────┼──────────────────────────────────────────────┘
                │
                │ WebSocket / Jupyter Comm
                │
┌───────────────┼──────────────────────────────────────────────┐
│               ▼                                               │
│  ┌─────────────────────────────────────────────────────┐    │
│  │         Transport Layer (Pluggable)                  │    │
│  │  • DirectWebSocketTransport                          │    │
│  │  • MultiplexedWebSocketTransport                     │    │
│  │  • JupyterCommTransport                              │    │
│  └────────────┬────────────────────────────────────────┘    │
│               │                                               │
│  ┌────────────┴────────────────────────────────────────┐    │
│  │         DataPipe Instances (Python)                  │    │
│  │  pipe1 ───┐                                          │    │
│  │  pipe2 ───┼─→ Multiplexed over single connection    │    │
│  │  pipe3 ───┘                                          │    │
│  └──────────────────────────────────────────────────────┘   │
│                     Backend (Python)                         │
└──────────────────────────────────────────────────────────────┘
```

## File Structure

```
iclean/
├── bokeh/
│   └── sources/
│       ├── _data_pipe.py              # Original (for reference)
│       ├── _data_pipe_new.py          # Modified DataPipe
│       ├── data_pipe.ts               # Original TypeScript
│       ├── data_pipe_new.ts           # Modified TypeScript
│       └── transport/
│           ├── __init__.py
│           ├── transport.py           # Base transport abstraction
│           ├── direct_websocket.py    # Direct WebSocket (legacy compat)
│           ├── multiplexed_websocket.py   # Multiplexed WebSocket
│           └── jupyter_comm.py        # Jupyter comm transport
│
├── jupyter_extension/
│   ├── __init__.py
│   ├── jupyter_extension.py       # Jupyter Server Extension
│   ├── session_registry.py        # Session management
│   └── setup.py                   # Extension setup
│
├── remote_session.py              # RemoteICLeanSession API
│
└── docs/
    ├── INSTALLATION.md            # Installation guide
    ├── EXAMPLES.md                # Usage examples
    └── API.md                     # API documentation
```

## Components

### 1. Transport Abstraction Layer (`transport/transport.py`)

**Purpose**: Provides a pluggable interface for different communication mechanisms.

**Key Classes**:
- `DataPipeTransport`: Abstract base class for all transports
- `TransportManager`: Singleton managing transport instances
- `TransportMessage`: Standard message format across transports

**Usage**:
```python
from iclean.bokeh.sources.transport import TransportManager, generate_transport_key

manager = TransportManager()
transport = manager.get_transport(
    transport_key='my_key',
    transport_type='multiplexed',
    address=('localhost', 5000)
)
```

### 2. Multiplexed WebSocket Transport (`transport/multiplexed_websocket.py`)

**Purpose**: Allows multiple DataPipes to share a single WebSocket connection.

**Features**:
- Reduced network resource usage
- Session conflict detection
- Automatic message routing by pipe_id
- Connection recovery

**Usage**:
```python
pipe1 = DataPipe(
    address=('localhost', 5000),
    transport_mode='multiplexed',
    transport_key='shared'
)
pipe2 = DataPipe(
    address=('localhost', 5000),
    transport_mode='multiplexed',
    transport_key='shared'  # Shares connection with pipe1
)
```

### 3. Jupyter Comm Transport (`transport/jupyter_comm.py`)

**Purpose**: Enables communication via Jupyter kernel comm system.

**Features**:
- No direct WebSocket needed
- Works through Jupyter's infrastructure
- Supports remote kernels
- Automatic authentication via Jupyter

**Usage**:
```python
pipe = DataPipe(
    transport_mode='jupyter',
    session_id='my_session',
    comm_manager=ipython.kernel.comm_manager
)
```

### 4. Session Registry (`jupyter_extension/session_registry.py`)

**Purpose**: Maps session IDs to kernel IDs for routing.

**Features**:
- Persistent session storage
- Session lifecycle management
- Cleanup of stale sessions

**Usage**:
```python
from iclean.jupyter_extension.session_registry import register_session, get_session

register_session('session-123', 'kernel-abc', host='cluster.edu')
info = get_session('session-123')
```

### 5. Jupyter Server Extension (`jupyter_extension/jupyter_extension.py`)

**Purpose**: Bridges WebSocket connections to Jupyter kernel comms.

**Endpoints**:
- `GET /iclean/status` - Extension status
- `WS /iclean/ws/<session_id>` - WebSocket for session
- `GET /iclean/session/<session_id>` - Session info
- `DELETE /iclean/session/<session_id>` - Delete session

**Features**:
- WebSocket to comm bridging
- Authentication via Jupyter
- Automatic session cleanup
- Error handling and reporting

### 6. Modified DataPipe (`_data_pipe_new.py`)

**Purpose**: Enhanced DataPipe with transport support.

**New Properties**:
- `pipe_id`: Unique identifier for this DataPipe
- `session_id`: Session identifier
- `connection_mode`: 'direct', 'multiplexed', 'jupyter_remote'
- `jupyter_ws_url`: WebSocket URL for Jupyter connections

## Integration Path

### Phase 1: Foundation (Week 1)
**Goal**: Add transport abstraction while maintaining backward compatibility

1. **Copy files into your project**:
   ```bash
   mkdir -p iclean/bokeh/sources/transport
   cp transport/*.py iclean/bokeh/sources/transport/
   ```

2. **Test backward compatibility**:
   ```python
   # Existing code should work unchanged
   from iclean.bokeh.sources import DataPipe
   pipe = DataPipe(address=('localhost', 5000))
   # Should work exactly as before
   ```

3. **Verify tests pass**:
   ```bash
   pytest tests/test_datapipe.py
   ```

### Phase 2: Multiplexing (Week 2)
**Goal**: Enable multiple DataPipes to share connections

1. **Update DataPipe instantiation**:
   ```python
   # Change from
   pipe1 = DataPipe(address=('localhost', 5001))
   pipe2 = DataPipe(address=('localhost', 5002))
   
   # To
   pipe1 = DataPipe(address=('localhost', 5000), 
                    transport_mode='multiplexed',
                    transport_key='shared')
   pipe2 = DataPipe(address=('localhost', 5000),
                    transport_mode='multiplexed', 
                    transport_key='shared')
   ```

2. **Test multiplexing**:
   ```python
   # All pipes should communicate over single WebSocket
   # Check with netstat or browser dev tools
   ```

3. **Update TypeScript**:
   ```bash
   cp data_pipe_new.ts src/bokeh/sources/data_pipe.ts
   npm run build
   ```

### Phase 3: Jupyter Extension (Week 2-3)
**Goal**: Install and configure Jupyter Server Extension

1. **Install extension**:
   ```bash
   cd jupyter-extension
   pip install -e .
   jupyter server extension enable iclean.jupyter_extension
   ```

2. **Test extension**:
   ```bash
   jupyter lab
   # Open http://localhost:8888/iclean/status
   # Should see {"status": "ok", ...}
   ```

3. **Test Jupyter mode locally**:
   ```python
   # In Jupyter notebook
   from iclean.bokeh.sources import DataPipe
   pipe = DataPipe(transport_mode='jupyter')
   ```

### Phase 4: Remote Mode (Week 3-4)
**Goal**: Enable remote kernel connections

1. **Test remote kernel**:
   ```python
   # Start kernel on remote machine
   # ssh cluster.edu
   # jupyter kernel --kernel=python3
   
   # Connect from local
   from iclean import RemoteICLeanSession
   session = RemoteICLeanSession(
       kernel_connection_info='kernel-123.json',
       remote_host='cluster.edu:8888'
   )
   ```

2. **Test reconnection**:
   ```python
   # Close browser
   # Later...
   session = RemoteICLeanSession.reconnect('session-id')
   ```

### Phase 5: Polish (Week 4+)
**Goal**: Production readiness

1. **Add error handling**
2. **Add logging**
3. **Add unit tests**
4. **Add integration tests**
5. **Document APIs**
6. **Performance testing**

## Usage Examples

### Example 1: Python CLI (Multiplexed)

```python
from iclean import ICLean

# Create application with multiple DataPipes
app = ICLean(transport_mode='multiplexed')

# All DataPipes automatically share one WebSocket
app.run()
```

### Example 2: Jupyter Notebook (Local)

```python
# In Jupyter notebook cell
from iclean import ICLean

app = ICLean(transport_mode='jupyter')
app.display()  # UI appears in cell
```

### Example 3: Remote Kernel (On-site)

```python
# On cluster (via SSH)
jupyter lab --no-browser --port=8888

# On local machine
from iclean import RemoteICLeanSession

session = RemoteICLeanSession(
    kernel_connection_info='~/Downloads/kernel-abc.json',
    remote_host='cluster.example.edu:8888',
    jupyter_token='my_secure_token'
)

# Work with UI...
# Close laptop, go home
```

### Example 4: Remote Kernel (At Home - Reconnect)

```python
# At home, reconnect to same session
from iclean import RemoteICLeanSession

# List available sessions
sessions = RemoteICLeanSession.list_sessions()
print("Available sessions:", sessions.keys())

# Reconnect
session = RemoteICLeanSession.reconnect(
    session_id='the-session-id',
    jupyter_token='my_secure_token'
)

# Continue where you left off
print(session.status())
```

### Example 5: Multiple Independent Sessions

```python
# Start multiple analysis sessions
session1 = RemoteICLeanSession(
    kernel_connection_info='kernel-1.json',
    remote_host='cluster.edu:8888'
)

session2 = RemoteICLeanSession(
    kernel_connection_info='kernel-2.json',
    remote_host='cluster.edu:8888'
)

# Each has independent backend and UI
```

## API Reference

### DataPipe Constructor

```python
DataPipe(
    address: tuple[str, int] = None,
    abort: Callable = None,
    transport_mode: str = 'auto',  # 'auto', 'direct', 'multiplexed', 'jupyter'
    transport_key: str = None,
    session_id: str = None,
    comm_manager = None,
    **kwargs
)
```

### RemoteICLeanSession Constructor

```python
RemoteICLeanSession(
    kernel_connection_info: str | Path | dict = None,
    remote_host: str = None,
    session_id: str = None,
    jupyter_token: str = None,
    **backend_kwargs
)
```

### RemoteICLeanSession Methods

```python
# Reconnect to existing session
session = RemoteICLeanSession.reconnect(session_id, remote_host=None, jupyter_token=None)

# Get session status
status = session.status()

# Shutdown session
session.shutdown(stop_kernel=False)

# List all sessions
sessions = RemoteICLeanSession.list_sessions()

# Cleanup old sessions
removed = RemoteICLeanSession.cleanup_old_sessions(max_age=604800)
```

## Testing

### Unit Tests

```bash
# Run all tests
pytest tests/

# Run specific test
pytest tests/test_transport.py -v

# Run with coverage
pytest --cov=iclean tests/
```

### Integration Tests

```python
# Test multiplexed transport
def test_multiplexed_transport():
    pipe1 = DataPipe(
        address=('localhost', 5555),
        transport_mode='multiplexed',
        transport_key='test'
    )
    pipe2 = DataPipe(
        address=('localhost', 5555),
        transport_mode='multiplexed',
        transport_key='test'
    )
    
    # Both should share same transport
    assert pipe1._DataPipe__transport is pipe2._DataPipe__transport

# Test Jupyter mode
def test_jupyter_mode():
    try:
        pipe = DataPipe(transport_mode='jupyter')
        assert pipe._DataPipe__transport is not None
    except RuntimeError:
        pytest.skip("Not in Jupyter environment")

# Test remote session
def test_remote_session():
    # Requires running kernel
    session = RemoteICLeanSession(
        kernel_connection_info='test_kernel.json',
        remote_host='localhost:8888'
    )
    
    assert session.status()['backend_started']
    session.shutdown()
```

## Configuration

### Jupyter Server Config

`~/.jupyter/jupyter_server_config.py`:

```python
# Keep kernels alive
c.MappingKernelManager.cull_idle_timeout = 0
c.MappingKernelManager.cull_connected = False

# Session storage
c.ICLeanExtension.session_storage_path = '~/.iclean/sessions.json'

# Allowed origins (adjust for your setup)
c.ICLeanExtension.allowed_origins = [
    'http://localhost:*',
    'https://yourdomain.com'
]

# Cleanup interval (seconds)
c.ICLeanExtension.cleanup_interval = 3600  # 1 hour
```

### Environment Variables

```bash
# Session storage directory
export ICLEAN_SESSION_DIR=~/.iclean/sessions

# Jupyter token for remote connections
export JUPYTER_TOKEN=your_secure_token

# Enable debug logging
export ICLEAN_DEBUG=1

# Set default transport mode
export ICLEAN_TRANSPORT_MODE=multiplexed
```

## Troubleshooting

### Common Issues

**1. "Extension not found"**
```bash
# Check if installed
jupyter server extension list

# Reinstall
pip uninstall iclean-jupyter-extension
pip install -e jupyter-extension/
jupyter server extension enable iclean.jupyter_extension
```

**2. "WebSocket connection failed"**
```python
# Check if port is available
import socket
sock = socket.socket()
try:
    sock.bind(('localhost', 5000))
    print("Port available")
except OSError:
    print("Port in use - try different port")
sock.close()

# Check firewall settings
# On Linux: sudo ufw allow 5000
# On Mac: Check System Preferences > Security
```

**3. "Session not found"**
```python
# List sessions
from iclean import RemoteICLeanSession
sessions = RemoteICLeanSession.list_sessions()
print(sessions)

# Clean up and retry
RemoteICLeanSession.cleanup_old_sessions()
```

**4. "Kernel connection refused"**
```bash
# Verify kernel is running
jupyter kernel list
jupyter console --existing kernel-123.json

# Check connection file
cat ~/.local/share/jupyter/runtime/kernel-123.json
```

**5. "Session conflict detected"**
```javascript
// Clear browser storage
localStorage.clear()

// Or specific key
localStorage.removeItem('cubevis_datapipe_localhost_5000')
```

### Debug Mode

```python
import logging

# Enable debug logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Transport layer logging
logger = logging.getLogger('iclean.transport')
logger.setLevel(logging.DEBUG)

# Jupyter extension logging
logger = logging.getLogger('iclean.jupyter_extension')
logger.setLevel(logging.DEBUG)
```

## Performance Considerations

### Multiplexed vs Direct

**Multiplexed** (Recommended):
- ✅ Lower resource usage
- ✅ Fewer network connections
- ✅ Better for many DataPipes
- ⚠️ Slightly more complex error handling

**Direct**:
- ✅ Simple, tried and tested
- ✅ Isolated failures
- ❌ More network resources
- ❌ More WebSocket connections

### Jupyter Comm vs WebSocket

**Jupyter Comm** (For Jupyter):
- ✅ No separate WebSocket needed
- ✅ Works with JupyterHub
- ✅ Built-in authentication
- ⚠️ Requires Jupyter environment

**WebSocket** (For CLI):
- ✅ Lower latency
- ✅ More control
- ✅ Works anywhere
- ❌ Needs firewall configuration

## Security

### Best Practices

1. **Use HTTPS/WSS in production**
2. **Set strong Jupyter tokens**
3. **Configure CORS properly**
4. **Use firewall rules**
5. **Rotate credentials regularly**
6. **Clean up old sessions**
7. **Monitor for suspicious activity**

### Example Secure Configuration

```python
# jupyter_server_config.py
c.ServerApp.token = 'very_long_random_string_here'
c.ServerApp.password_required = True
c.ServerApp.allow_origin = 'https://yourdomain.com'
c.ServerApp.allow_credentials = True

# SSL configuration
c.ServerApp.certfile = '/path/to/cert.pem'
c.ServerApp.keyfile = '/path/to/key.pem'

# Restrict to specific host
c.ServerApp.ip = '127.0.0.1'  # localhost only
# or
c.ServerApp.ip = '0.0.0.0'  # all interfaces (with firewall!)
```

## Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

## License

[Your License Here]

## Support

- Documentation: [https://iclean.readthedocs.io](https://iclean.readthedocs.io)
- Issues: [https://github.com/yourusername/iclean/issues](https://github.com/yourusername/iclean/issues)
- Discussions: [https://github.com/yourusername/iclean/discussions](https://github.com/yourusername/iclean/discussions)

## Acknowledgments

Built on:
- Bokeh for visualization
- Jupyter for kernel infrastructure
- WebSockets for communication
- Tornado for async networking Features**:
- Pluggable transport system
- Auto-detection of environment
- Backward compatible with legacy code
- Enhanced error handling

**New Parameters**:
- `transport_mode`: 'auto', 'direct', 'multiplexed', 'jupyter'
- `transport_key`: For sharing transports
- `session_id`: Session identifier
- `comm_manager`: For Jupyter mode

### 7. Remote Session Manager (`remote_session.py`)

**Purpose**: User-facing API for remote sessions.

**Features**:
- Start backends on remote kernels
- Connect local UIs to remote backends
- Session reconnection
- Session lifecycle management

**Usage**:
```python
# Start new remote session
session = RemoteICLeanSession(
    kernel_connection_info='kernel-123.json',
    remote_host='cluster.edu:8888'
)

# Reconnect to existing
session = RemoteICLeanSession.reconnect('session-xyz')

# Check status
print(session.status())

# Shutdown
session.shutdown(stop_kernel=False)
```

### 8. TypeScript DataPipe (`data_pipe_new.ts`)

**Purpose**: Frontend counterpart with transport support.

**New Features**:
- Jupyter remote connection mode
- Enhanced error handling
- Better session management
- Message routing for multiplexing

**New