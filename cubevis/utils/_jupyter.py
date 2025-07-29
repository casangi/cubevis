
#def is_notebook() -> bool:
#    try:
#        shell = get_ipython().__class__.__name__
#        if shell == 'ZMQInteractiveShell':
#            return True   # Jupyter notebook or qtconsole
#        elif shell == 'TerminalInteractiveShell':
#            return False  # Terminal running IPython
#        else:
#            if get_ipython().__class__.__module__ == 'google.colab._shell':
#                return True   # Google Colab
#            else:
#              return False  # Other type (?)
#    except NameError:
#        return False

def is_interactive_jupyter( ) -> bool:
    """
    Detect if running in an interactive Jupyter notebook with frontend connection.

    This function distinguishes between:
    - Interactive Jupyter notebook/lab with frontend (returns True)
    - Standalone Jupyter kernel without frontend (returns False)
    - Regular Python interpreter (returns False)

    Returns:
        bool: True if running in interactive Jupyter with frontend, False otherwise
    """
    try:
        from IPython import get_ipython
        ipython = get_ipython()

        if ipython is None:
            return False

        # Check if we're in a ZMQ-based shell (kernel)
        if ipython.__class__.__name__ != 'ZMQInteractiveShell':
            return False

        # Check for active frontend connection
        if hasattr(ipython, 'kernel') and ipython.kernel is not None:
            kernel = ipython.kernel

            # Method 1: Check if there are active connections
            if hasattr(kernel, 'shell_socket') and kernel.shell_socket is not None:
                # For newer Jupyter versions, check connection count
                if hasattr(kernel, 'connection_count'):
                    return kernel.connection_count > 0

                # For older versions, check if socket is connected
                try:
                    # Try to get socket state - if it fails, likely no frontend
                    socket_state = kernel.shell_socket.closed
                    return not socket_state
                except AttributeError:
                    pass

            # Method 2: Check parent message (indicates interactive execution)
            if hasattr(kernel, 'get_parent') and callable(kernel.get_parent):
                try:
                    parent = kernel.get_parent()
                    # If there's a parent message, we're likely in interactive mode
                    return parent is not None and len(parent) > 0
                except Exception:
                    pass

            # Method 3: Check for execution context
            if hasattr(kernel, '_parent_ident') and kernel._parent_ident:
                return True

        # Fallback: Check for common Jupyter notebook environment indicators
        # This catches cases where kernel introspection doesn't work
        import os
        jupyter_indicators = [
            'JPY_PARENT_PID',  # JupyterLab/Notebook sets this
            'JUPYTER_RUNTIME_DIR',
        ]

        for indicator in jupyter_indicators:
            if indicator in os.environ:
                # Additional check: see if we can import notebook-specific modules
                try:
                    import IPython.display
                    # If we can import display and have env indicators, likely interactive
                    return True
                except ImportError:
                    pass

        return False

    except ImportError:
        return False
