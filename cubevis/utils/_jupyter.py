import os
import sys
import logging

logger = logging.getLogger(__name__)

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

def is_colab():
    """
    Detects if the current environment is Google Colab.
    Combines module check (process-level) and env-var check (VM-level).
    """
    # 1. Check for the Colab-specific environment variable (VM level)
    # 2. Check for the google.colab module (Python process level)
    return os.getenv("COLAB_RELEASE_TAG") is not None or 'google.colab' in sys.modules

def is_interactive_jupyter( ) -> bool:
    """
    Detect if running in an interactive Jupyter notebook with frontend connection.

    This function distinguishes between:
    - Interactive Jupyter notebook/lab with frontend (returns True)
    - Standalone Jupyter kernel without frontend (returns False)
    - Regular Python interpreter (returns False)
    - Support Google Colab and legacy Jupyter versions.

    Returns:
        bool: True if running in interactive Jupyter with frontend, False otherwise
    """
    try:
        from IPython import get_ipython
        ipython = get_ipython()

        if ipython is None:
            logger.debug(f"\tis_interactive_jupyter<1>: False")
            return False

        # Allow Colab's 'Shell' class in addition to standard 'ZMQInteractiveShell'
        shell_class = ipython.__class__.__name__
        is_colab = 'google.colab' in sys.modules

        # Check if we're in a ZMQ-based shell (kernel) or Colab's 'Shell'
        if shell_class not in ['ZMQInteractiveShell', 'Shell'] and not is_colab:
            logger.debug(f"\tis_interactive_jupyter<2>: False")
            return False

        # If we are in Colab, the existence of the shell itself implies
        # an interactive frontend, as Colab sessions are inherently interactive.
        if is_colab:
            return True

        # Check for active frontend connection (Standard Jupyter/Legacy)
        if hasattr(ipython, 'kernel') and ipython.kernel is not None:
            kernel = ipython.kernel

            # Method 1: Check if there are active connections
            if hasattr(kernel, 'shell_socket') and kernel.shell_socket is not None:
                # For newer Jupyter versions, check connection count
                if hasattr(kernel, 'connection_count'):
                    logger.debug(f"\tis_interactive_jupyter<3>: {kernel.connection_count > 0}")
                    if kernel.connection_count > 0:
                        return True

                # For older versions, check if socket is connected
                try:
                    # Try to get socket state - if it fails, likely no frontend
                    logger.debug(f"\tis_interactive_jupyter<4>: {not socket_state}")
                    if not kernel.shell_socket.closed:
                        return True
                except AttributeError:
                    pass

            # Method 2: Check parent message (indicates interactive execution)
            if hasattr(kernel, 'get_parent') and callable(kernel.get_parent):
                try:
                    parent = kernel.get_parent()
                    # If there's a parent message, we're likely in interactive mode
                    logger.debug(f"\tis_interactive_jupyter<5>: {parent is not None and len(parent) > 0}")
                    if parent is not None and len(parent) > 0:
                        return True
                except Exception:
                    pass

            # Method 3: Check for execution context
            if hasattr(kernel, '_parent_ident') and kernel._parent_ident:
                logger.debug(f"\tis_interactive_jupyter<6>: True")
                return True

        # Fallback: Check for environment indicators (Legacy/Containerized Jupyter)
        jupyter_indicators = [
            'JPY_PARENT_PID',       # JupyterLab/Notebook sets this
            'JUPYTER_RUNTIME_DIR',
            'COLAB_RELEASE_DEVICE', # Additional Colab-specific env check
            'COLAB_GPU'             # Additional Colab-specific env check
        ]

        for indicator in jupyter_indicators:
            if indicator in os.environ:
                # Additional check: see if we can import notebook-specific modules
                try:
                    import IPython.display
                    # If we can import display and have env indicators, likely interactive
                    logger.debug(f"\tis_interactive_jupyter<7>: True")
                    return True
                except ImportError:
                    pass

        logger.debug(f"\tis_interactive_jupyter<8>: False")
        return False

    except (ImportError, Exception):
        logger.debug(f"\tis_interactive_jupyter<9>: False")
        return False
