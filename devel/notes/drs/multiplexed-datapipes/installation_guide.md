# iclean Transport System - Installation and Configuration Guide

This guide covers installation and configuration of the new transport system for iclean, including support for multiplexed WebSockets, Jupyter comm transport, and remote kernel sessions.

## Table of Contents

1. [Installation](#installation)
2. [Configuration](#configuration)
3. [Usage Examples](#usage-examples)
4. [Migration from Legacy DataPipe](#migration-from-legacy-datapipe)
5. [Troubleshooting](#troubleshooting)

## Installation

### Basic Installation

For basic iclean functionality with the new transport system:

```bash
pip install iclean
```

### Jupyter Extension Installation

For Jupyter integration and remote kernel support:

```bash
# Install the extension
pip install iclean-jupyter-extension

# Enable the extension
jupyter server extension enable iclean.jupyter_extension

# Verify installation
jupyter server extension list
```

You should see `iclean.jupyter_extension` listed as enabled.

### Development Installation

For development or testing:

```bash
# Clone repository
git clone https://github.com/yourusername/iclean.git
cd iclean

# Install in editable mode
pip install -e .

# Install Jupyter extension in editable mode
cd jupyter-extension
pip install -e .
jupyter server extension enable --sys-prefix iclean.jupyter_extension
```

## Configuration

### Jupyter Server Configuration

Create or edit `~/.jupyter/jupyter_server_config.py`:

```python
# Disable kernel culling to keep sessions alive
c.MappingKernelManager.cull_idle_timeout = 0
c.MappingKernelManager.cull_connected = False

# Optional: Configure session storage path
c.ICLeanExtension.session_storage_path = '/path/to/sessions.json'

# Optional: Configure allowed origins for CORS
c.ICLeanExtension.allowed_origins = ['http://localhost:*', 'https://example.com']
```

### JupyterHub Configuration

For JupyterHub deployments, add to `jupyterhub_config.py`:

```python
# Ensure iclean extension is loaded
c.Spawner.default_url = '/lab'
c.Spawner.cmd = ['jupyter-labhub']

# Configure network settings if needed
c.Spawner.args = ['--ServerApp.allow_origin=*']
```

### Environment Variables

```bash
# Set custom session storage location
export ICLEAN_SESSION_DIR=~/.iclean/sessions

# Set Jupyter token for remote connections
export JUPYTER_TOKEN=your_token_here
```

## Usage Examples

### 1. Python CLI Mode (Local)

Traditional single-machine usage:

```python
from iclean import ICLean

# Automatically uses multiplexed transport
app = ICLean()
app.run()
```

### 2. Jupyter Notebook Mode (Local Kernel)

Using iclean in a Jupyter notebook:

```python
# In notebook cell
from iclean import ICLean

app = ICLean(transport_mode='jupyter')
app.run()
# UI appears in notebook cell
```

### 3. Remote Kernel Mode

#### Starting a Remote Kernel

On the remote machine (cluster):

```bash
# Start Jupyter server
jupyter lab --no-browser --port=8888

# Or start just a kernel
jupyter kernel --kernel=python3
# Note the connection file location printed
```

#### Connecting from Local Machine

```python
from iclean import RemoteICLeanSession

# Option 1: Connect with connection file
session = RemoteICLeanSession(
    kernel_connection_info='~/Downloads/kernel-12345.json',
    remote_host='cluster.example.edu:8888',
    jupyter_token='your_token_here'
)

# Option 2: Connect to existing session
session = RemoteICLeanSession.reconnect('session-abc-123')

# Check status
print(session.status())

# Later, shutdown (keeps kernel running)
session.shutdown(stop_kernel=False)
```

### 4. Multiple DataPipes (Multiplexed)

Using multiple DataPipes efficiently:

```python
from iclean.bokeh.sources import DataPipe

# All share the same WebSocket connection
pipe1 = DataPipe(
    address=('localhost', 5000),
    transport_mode='multiplexed',
    transport_key='shared_connection'
)

pipe2 = DataPipe(
    address=('localhost', 5000),
    transport_mode='multiplexed',
    transport_key='shared_connection'  # Same key = shared connection
)

pipe3 = DataPipe(
    address=('localhost', 5000),
    transport_mode='multiplexed',
    transport_key='shared_connection'
)
```

### 5. Custom Transport Configuration

Advanced configuration:

```python
from iclean.bokeh.sources import DataPipe
from iclean.bokeh.sources.transport import TransportManager

# Create custom transport
transport_manager = TransportManager()
custom_transport = transport_manager.get_transport(
    transport_key='my_custom_key',
    transport_type='multiplexed',
    address=('0.0.0.0', 5555)
)

# Use in DataPipe
pipe = DataPipe(
    address=('0.0.0.0', 5555),
    transport_mode='multiplexed',
    transport_key='my_custom_key'
)
```

## Migration from Legacy DataPipe

### Backward Compatibility

The new DataPipe is **100% backward compatible**. Existing code will work without changes:

```python
# Old code still works
from iclean.bokeh.sources import DataPipe

pipe = DataPipe(address=('localhost', 5000))
# Automatically uses 'direct' mode (one WebSocket per pipe)
```

### Migration Steps

#### Step 1: Update Transport Mode

Change to multiplexed for better resource usage:

```python
# Before
pipe = DataPipe(address=('localhost', 5000))

# After
pipe = DataPipe(
    address=('localhost', 5000),
    transport_mode='multiplexed'
)
```

#### Step 2: Share Connections

If you have multiple DataPipes:

```python
# Before - each pipe had its own WebSocket
pipe1 = DataPipe(address=('localhost', 5000))
pipe2 = DataPipe(address=('localhost', 5001))  # Different ports!
pipe3 = DataPipe(address=('localhost', 5002))

# After - all share one WebSocket
shared_key = 'my_app_connection'
pipe1 = DataPipe(
    address=('localhost', 5000),
    transport_mode='multiplexed',
    transport_key=shared_key
)
pipe2 = DataPipe(
    address=('localhost', 5000),
    transport_mode='multiplexed',
    transport_key=shared_key
)
pipe3 = DataPipe(
    address=('localhost', 5000),
    transport_mode='multiplexed',
    transport_key=shared_key
)
```

#### Step 3: Enable Jupyter Mode

For notebook usage:

```python
# Before - direct WebSocket in notebook
pipe = DataPipe(address=('localhost', 5000))

# After - use Jupyter comm
pipe = DataPipe(
    transport_mode='jupyter',
    session_id='my_session'
)
```

### Testing Migration

```python
# Test script to verify migration
def test_migration():
    from iclean.bokeh.sources import DataPipe
    
    # Test direct mode (backward compatibility)
    pipe1 = DataPipe(address=('localhost', 5000))
    assert pipe1._DataPipe__transport is not None
    
    # Test multiplexed mode
    pipe2 = DataPipe(
        address=('localhost', 5000),
        transport_mode='multiplexed'
    )
    assert pipe2._DataPipe__transport is not None
    
    # Test Jupyter mode (if in Jupyter)
    try:
        pipe3 = DataPipe(transport_mode='jupyter')
        assert pipe3._DataPipe__transport is not None
    except RuntimeError as e:
        print(f"Jupyter mode not available: {e}")
    
    print("All tests passed!")

if __name__ == '__main__':
    test_migration()
```

## Troubleshooting

### Common Issues

#### 1. Extension Not Loading

```bash
# Check if extension is enabled
jupyter server extension list

# If not listed, enable it
jupyter server extension enable iclean.jupyter_extension

# Restart Jupyter server
jupyter lab --port=8888
```

#### 2. WebSocket Connection Refused

```python
# Check firewall settings
# Ensure port is open

# Try different port
pipe = DataPipe(address=('localhost', 5001))

# Check if port is in use
import socket
sock = socket.socket()
try:
    sock.bind(('localhost', 5000))
    print("Port available")
except OSError:
    print("Port in use")
finally:
    sock.close()
```

#### 3. Session Not Found

```python
from iclean import RemoteICLeanSession

# List all sessions
sessions = RemoteICLeanSession.list_sessions()
print("Available sessions:", sessions.keys())

# Clean up old sessions
removed = RemoteICLeanSession.cleanup_old_sessions()
print(f"Removed {removed} old sessions")
```

#### 4. Kernel Connection Issues

```python
# Verify kernel is running
jupyter kernelspec list
jupyter console --existing kernel-12345.json

# Check connection info
import json
with open('kernel-12345.json') as f:
    info = json.load(f)
    print("Kernel info:", info)
```

#### 5. Session Conflicts

If you see session conflict messages:

```python
# Close all browser windows
# Clear localStorage
# In browser console:
localStorage.clear()

# Or clear specific key
localStorage.removeItem('cubevis_datapipe_localhost_5000')
```

### Debug Mode

Enable debug logging:

```python
import logging

# Enable debug logging for iclean
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('iclean')
logger.setLevel(logging.DEBUG)

# Enable debug for Jupyter extension
import sys
sys.path.insert(0, '/path/to/iclean/jupyter_extension')
```

### Getting Help

- Check logs: `~/.iclean/iclean.log`
- Check Jupyter logs: `~/.jupyter/jupyter_server.log`
- Enable verbose mode: `export ICLEAN_VERBOSE=1`
- File issues: https://github.com/yourusername/iclean/issues

## Performance Tips

1. **Use multiplexed mode** for applications with multiple DataPipes
2. **Set appropriate timeouts** for long-running operations
3. **Clean up old sessions** periodically
4. **Monitor kernel memory** usage for long-running sessions
5. **Use session_id** consistently for reconnection

## Security Considerations

1. **Always use HTTPS/WSS** in production
2. **Set strong Jupyter tokens**
3. **Configure allowed_origins** appropriately
4. **Use firewall rules** to restrict access
5. **Regularly rotate tokens** and credentials
6. **Clean up stale sessions** to prevent resource leaks

## Next Steps

- Read the [API Documentation](API.md)
- See [Advanced Examples](EXAMPLES.md)
- Join the [Community Forum](https://forum.example.com)
