"""
Test script for transport startup in various contexts.

This verifies that DataPipe can be created without event loop errors
in different scenarios.
"""

import sys
import time


def test_no_event_loop():
    """Test 1: Create DataPipe when no event loop exists."""
    print("=" * 60)
    print("Test 1: No Event Loop Context")
    print("=" * 60)
    
    # This is the typical case during module import
    from iclean.bokeh.sources import DataPipe
    
    try:
        pipe = DataPipe(
            address=('localhost', 5000),
            transport_mode='multiplexed'
        )
        print("✓ DataPipe created successfully")
        print(f"  Pipe ID: {pipe.pipe_id}")
        print(f"  Session ID: {pipe.session_id}")
        time.sleep(1)  # Give transport time to start
        print(f"  Transport connected: {pipe._DataPipe__transport.is_connected()}")
        return True
    except Exception as e:
        print(f"✗ Failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_with_event_loop():
    """Test 2: Create DataPipe with existing event loop."""
    print("\n" + "=" * 60)
    print("Test 2: With Existing Event Loop")
    print("=" * 60)
    
    import asyncio
    from iclean.bokeh.sources import DataPipe
    
    async def create_pipe():
        try:
            pipe = DataPipe(
                address=('localhost', 5001),
                transport_mode='multiplexed'
            )
            print("✓ DataPipe created in async context")
            print(f"  Pipe ID: {pipe.pipe_id}")
            await asyncio.sleep(1)
            print(f"  Transport connected: {pipe._DataPipe__transport.is_connected()}")
            return True
        except Exception as e:
            print(f"✗ Failed: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    try:
        result = asyncio.run(create_pipe())
        return result
    except Exception as e:
        print(f"✗ Outer failure: {e}")
        return False


def test_multiple_pipes_shared_transport():
    """Test 3: Multiple DataPipes sharing one transport."""
    print("\n" + "=" * 60)
    print("Test 3: Multiple Pipes, Shared Transport")
    print("=" * 60)
    
    from iclean.bokeh.sources import DataPipe
    
    try:
        pipes = []
        shared_key = 'test_shared'
        
        for i in range(3):
            pipe = DataPipe(
                address=('localhost', 5002),
                transport_mode='multiplexed',
                transport_key=shared_key
            )
            pipes.append(pipe)
            print(f"✓ Created pipe {i+1}: {pipe.pipe_id[:8]}...")
        
        time.sleep(1)
        
        # Verify they share transport
        transport1 = pipes[0]._DataPipe__transport
        transport2 = pipes[1]._DataPipe__transport
        transport3 = pipes[2]._DataPipe__transport
        
        if transport1 is transport2 is transport3:
            print("✓ All pipes share same transport instance")
        else:
            print("✗ Pipes have different transport instances")
            return False
        
        print(f"  Transport connected: {transport1.is_connected()}")
        return True
        
    except Exception as e:
        print(f"✗ Failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_threading_context():
    """Test 4: Create DataPipe in thread."""
    print("\n" + "=" * 60)
    print("Test 4: Threading Context")
    print("=" * 60)
    
    import threading
    from iclean.bokeh.sources import DataPipe
    
    result = {'success': False, 'error': None}
    
    def create_in_thread():
        try:
            pipe = DataPipe(
                address=('localhost', 5003),
                transport_mode='multiplexed'
            )
            print(f"✓ DataPipe created in thread: {pipe.pipe_id[:8]}...")
            time.sleep(1)
            print(f"  Transport connected: {pipe._DataPipe__transport.is_connected()}")
            result['success'] = True
        except Exception as e:
            result['error'] = e
            import traceback
            traceback.print_exc()
    
    thread = threading.Thread(target=create_in_thread)
    thread.start()
    thread.join(timeout=5)
    
    if result['success']:
        print("✓ Thread test passed")
        return True
    else:
        print(f"✗ Thread test failed: {result['error']}")
        return False


def test_jupyter_mode():
    """Test 5: Jupyter mode (if in Jupyter environment)."""
    print("\n" + "=" * 60)
    print("Test 5: Jupyter Mode")
    print("=" * 60)
    
    try:
        from IPython import get_ipython
        ipython = get_ipython()
        
        if ipython is None or not hasattr(ipython, 'kernel'):
            print("⊘ Not in Jupyter environment - skipping")
            return True
        
        from iclean.bokeh.sources import DataPipe
        
        pipe = DataPipe(
            transport_mode='jupyter',
            session_id='test-jupyter-session'
        )
        
        print("✓ DataPipe created in Jupyter mode")
        print(f"  Pipe ID: {pipe.pipe_id}")
        time.sleep(1)
        print(f"  Transport connected: {pipe._DataPipe__transport.is_connected()}")
        return True
        
    except ImportError:
        print("⊘ IPython not available - skipping")
        return True
    except Exception as e:
        print(f"✗ Failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests."""
    print("\n" + "#" * 60)
    print("# DataPipe Transport Startup Tests")
    print("#" * 60 + "\n")
    
    tests = [
        ("No Event Loop", test_no_event_loop),
        ("With Event Loop", test_with_event_loop),
        ("Multiple Pipes", test_multiple_pipes_shared_transport),
        ("Threading Context", test_threading_context),
        ("Jupyter Mode", test_jupyter_mode),
    ]
    
    results = []
    
    for name, test_func in tests:
        try:
            success = test_func()
            results.append((name, success))
        except Exception as e:
            print(f"\nUnexpected error in {name}: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))
        
        time.sleep(0.5)  # Brief pause between tests
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    passed = sum(1 for _, success in results if success)
    total = len(results)
    
    for name, success in results:
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"{status:8} - {name}")
    
    print(f"\nResults: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed!")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed")
        return 1


if __name__ == '__main__':
    sys.exit(main())
