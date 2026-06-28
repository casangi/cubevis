"""
Quick start examples for iclean DataPipe transport system.

Run individual examples:
    python quickstart.py --example basic
    python quickstart.py --example multiplexed
    python quickstart.py --example jupyter
    python quickstart.py --example remote
"""

import argparse
import sys


def example_basic():
    """Example 1: Basic DataPipe (backward compatible)"""
    print("=" * 60)
    print("Example 1: Basic DataPipe (Backward Compatible)")
    print("=" * 60)
    
    from iclean.bokeh.sources import DataPipe
    
    # Works exactly like before - automatic transport selection
    pipe = DataPipe(address=('localhost', 5000))
    
    print(f"✓ DataPipe created")
    print(f"  Pipe ID: {pipe.pipe_id}")
    print(f"  Session ID: {pipe.session_id}")
    print(f"  Transport mode: auto-detected")
    
    # Register a callback
    def handle_request(message):
        print(f"  Received: {message}")
        return {'status': 'ok', 'echo': message}
    
    pipe.register('test_id', handle_request)
    print(f"✓ Callback registered")
    
    print("\nDataPipe ready for use!")


def example_multiplexed():
    """Example 2: Multiplexed WebSocket (multiple DataPipes, one connection)"""
    print("=" * 60)
    print("Example 2: Multiplexed WebSocket")
    print("=" * 60)
    
    from iclean.bokeh.sources import DataPipe
    
    # All three DataPipes share a single WebSocket connection
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
    
    print(f"✓ Created 3 DataPipes sharing one connection")
    print(f"  Pipe 1 ID: {pipe1.pipe_id}")
    print(f"  Pipe 2 ID: {pipe2.pipe_id}")
    print(f"  Pipe 3 ID: {pipe3.pipe_id}")
    print(f"  Transport key: {shared_key}")
    
    # Verify they share the same transport
    transport1 = pipe1._DataPipe__transport
    transport2 = pipe2._DataPipe__transport
    transport3 = pipe3._DataPipe__transport
    
    assert transport1 is transport2 is transport3
    print(f"✓ Confirmed: All pipes use same transport instance")
    
    print("\nBenefit: Reduced network resource usage!")


def example_jupyter():
    """Example 3: Jupyter mode (works in notebooks)"""
    print("=" * 60)
    print("Example 3: Jupyter Comm Transport")
    print("=" * 60)
    
    try:
        from IPython import get_ipython
        ipython = get_ipython()
        
        if ipython is None or not hasattr(ipython, 'kernel'):
            print("❌ Not running in Jupyter environment")
            print("   Run this example in a Jupyter notebook:")
            print("   >>> from quickstart import example_jupyter")
            print("   >>> example_jupyter()")
            return
        
        from iclean.bokeh.sources import DataPipe
        
        # Use Jupyter comm for communication
        pipe = DataPipe(
            transport_mode='jupyter',
            session_id='my_jupyter_session'
        )
        
        print(f"✓ DataPipe created with Jupyter comm transport")
        print(f"  Pipe ID: {pipe.pipe_id}")
        print(f"  Session ID: {pipe.session_id}")
        print(f"  Comm manager: {pipe._DataPipe__comm_manager}")
        
        print("\nBenefit: No separate WebSocket needed!")
        print("Works seamlessly with JupyterHub and remote kernels.")
        
    except ImportError:
        print("❌ IPython not available")
        print("   Install with: pip install ipython jupyter")


def example_remote():
    """Example 4: Remote kernel session"""
    print("=" * 60)
    print("Example 4: Remote Kernel Session")
    print("=" * 60)
    
    from iclean import RemoteICLeanSession
    import os
    from pathlib import Path
    
    # Check if example kernel connection file exists
    kernel_file = Path.home() / 'kernel-example.json'
    
    if not kernel_file.exists():
        print("Creating example kernel connection file...")
        print(f"Location: {kernel_file}")
        
        # For demo, we'll create a local kernel
        print("\nStep 1: Starting a local kernel for demonstration...")
        
        try:
            from jupyter_client import KernelManager
            
            km = KernelManager()
            km.start_kernel()
            
            # Save connection info
            import json
            conn_info = km.get_connection_info()
            
            with open(kernel_file, 'w') as f:
                json.dump(conn_info, f, indent=2)
            
            print(f"✓ Kernel started and connection info saved to {kernel_file}")
            kernel_id = conn_info.get('kernel_id', 'unknown')
            print(f"  Kernel ID: {kernel_id}")
            
        except Exception as e:
            print(f"❌ Failed to start kernel: {e}")
            print("\nManual setup:")
            print("1. On remote machine: jupyter kernel --kernel=python3")
            print("2. Copy the connection file to your local machine")
            print("3. Update kernel_file path in this script")
            return
    
    # Create remote session
    print("\nStep 2: Creating remote session...")
    
    try:
        session = RemoteICLeanSession(
            kernel_connection_info=str(kernel_file),
            remote_host='localhost:8888',  # Change to your remote host
            session_id='demo_session'
        )
        
        print(f"✓ Remote session created")
        print(f"  Session ID: {session.session_id}")
        
        # Check status
        status = session.status()
        print(f"\nSession Status:")
        print(f"  Backend started: {status['backend_started']}")
        print(f"  UI created: {status['ui_created']}")
        print(f"  Kernel alive: {status['kernel_alive']}")
        
        print("\nStep 3: Demonstrating reconnection...")
        print("  (In real usage, you would close and reopen later)")
        
        # Save session info
        session._save_session_info()
        print(f"  ✓ Session info saved")
        
        # Simulate reconnection
        print("\n  Simulating reconnection...")
        reconnected = RemoteICLeanSession.reconnect(
            session_id='demo_session'
        )
        print(f"  ✓ Reconnected to session: {reconnected.session_id}")
        
        print("\nBenefit: Start session on-site, reconnect from home!")
        
        # Cleanup
        print("\nCleaning up demo session...")
        session.shutdown(stop_kernel=True)
        kernel_file.unlink()
        print("✓ Cleanup complete")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


def example_list_sessions():
    """Example 5: List and manage sessions"""
    print("=" * 60)
    print("Example 5: Session Management")
    print("=" * 60)
    
    from iclean import RemoteICLeanSession
    
    # List all sessions
    sessions = RemoteICLeanSession.list_sessions()
    
    print(f"Found {len(sessions)} saved session(s):")
    for session_id, info in sessions.items():
        print(f"\n  Session: {session_id}")
        print(f"    Created: {info.get('created_at', 'unknown')}")
        print(f"    Host: {info.get('remote_host', 'local')}")
        print(f"    Kernel: {info.get('kernel_id', 'unknown')}")
    
    if not sessions:
        print("  (No sessions found)")
    
    # Cleanup old sessions
    print("\nCleaning up old sessions (>7 days)...")
    removed = RemoteICLeanSession.cleanup_old_sessions(max_age=604800)
    print(f"✓ Removed {removed} old session(s)")


def example_transport_comparison():
    """Example 6: Compare different transport modes"""
    print("=" * 60)
    print("Example 6: Transport Mode Comparison")
    print("=" * 60)
    
    from iclean.bokeh.sources import DataPipe
    
    print("\n1. DIRECT MODE (original)")
    print("   - One WebSocket per DataPipe")
    print("   - Simplest, most tested")
    print("   - Higher resource usage")
    
    pipe_direct = DataPipe(
        address=('localhost', 5001),
        transport_mode='direct'
    )
    print(f"   ✓ Created: {pipe_direct.pipe_id}")
    
    print("\n2. MULTIPLEXED MODE (recommended)")
    print("   - Multiple DataPipes share one WebSocket")
    print("   - Lower resource usage")
    print("   - Better for many DataPipes")
    
    pipe_mux1 = DataPipe(
        address=('localhost', 5002),
        transport_mode='multiplexed',
        transport_key='shared'
    )
    pipe_mux2 = DataPipe(
        address=('localhost', 5002),
        transport_mode='multiplexed',
        transport_key='shared'
    )
    print(f"   ✓ Created: {pipe_mux1.pipe_id} (shares connection)")
    print(f"   ✓ Created: {pipe_mux2.pipe_id} (shares connection)")
    
    print("\n3. JUPYTER MODE (for notebooks)")
    print("   - Uses Jupyter comm system")
    print("   - No separate WebSocket needed")
    print("   - Best for Jupyter environments")
    
    try:
        from IPython import get_ipython
        if get_ipython() and hasattr(get_ipython(), 'kernel'):
            pipe_jupyter = DataPipe(transport_mode='jupyter')
            print(f"   ✓ Created: {pipe_jupyter.pipe_id}")
        else:
            print("   ⚠ Not in Jupyter environment - skipped")
    except:
        print("   ⚠ IPython not available - skipped")
    
    print("\n" + "=" * 60)
    print("Recommendation:")
    print("  - CLI apps: Use 'multiplexed'")
    print("  - Jupyter notebooks: Use 'jupyter'")
    print("  - Remote kernels: Use 'jupyter' + RemoteICLeanSession")
    print("  - Legacy code: Use 'auto' (backward compatible)")


def main():
    parser = argparse.ArgumentParser(description='iclean DataPipe Quick Start Examples')
    parser.add_argument(
        '--example',
        choices=['basic', 'multiplexed', 'jupyter', 'remote', 'list', 'compare', 'all'],
        default='all',
        help='Which example to run'
    )
    
    args = parser.parse_args()
    
    examples = {
        'basic': example_basic,
        'multiplexed': example_multiplexed,
        'jupyter': example_jupyter,
        'remote': example_remote,
        'list': example_list_sessions,
        'compare': example_transport_comparison,
    }
    
    if args.example == 'all':
        print("\n" + "=" * 60)
        print("RUNNING ALL EXAMPLES")
        print("=" * 60 + "\n")
        
        for name, func in examples.items():
            try:
                func()
                print("\n")
            except Exception as e:
                print(f"❌ Example '{name}' failed: {e}\n")
    else:
        examples[args.example]()


if __name__ == '__main__':
    main()
