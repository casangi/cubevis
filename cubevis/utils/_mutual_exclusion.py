import threading
from typing import Dict


class MutualExclusionManager:
    """
    Generic manager for enforcing mutual exclusion between different execution paths.

    Use this when you want to ensure only one of several mutually exclusive paths
    can be taken within a given context (thread).

    Example:
        mode_manager = MutualExclusionManager(
            name="display_mode",
            valid_modes={
                'notebook': "Cannot use Showable after displaying with bokeh.plotting.show().",
                'separate_tab': "Cannot use bokeh.plotting.show() after displaying with Showable."
            }
        )

        # In path A:
        mode_manager.set_mode('notebook')

        # In path B (will raise error if path A was already taken):
        mode_manager.set_mode('separate_tab')
    """

    def __init__(self, name: str, valid_modes: Dict[str, str]):
        """
        Args:
            name: Descriptive name for this manager (used in error messages)
            valid_modes: Dictionary mapping mode names to error messages to display
                        when that mode conflicts with an already-set mode.
        """
        self.name = name
        self.valid_modes = valid_modes
        self._local = threading.local()

    def set_mode(self, mode: str):
        """
        Set the execution mode. Raises RuntimeError if a different mode was already set.

        Args:
            mode: The mode identifier to set

        Raises:
            ValueError: If mode is not in valid_modes
            RuntimeError: If a different mode was already set in this context
        """
        if mode not in self.valid_modes:
            raise ValueError(
                f"Invalid mode '{mode}' for {self.name}. "
                f"Valid modes: {set(self.valid_modes.keys())}"
            )

        current_mode = self.get_mode()
        if current_mode is not None and current_mode != mode:
            # Use the error message associated with the mode being set
            error_message = self.valid_modes[mode]
            raise RuntimeError(error_message)

        self._local.mode = mode

    def get_mode(self) -> str | None:
        """
        Get the current mode, or None if no mode has been set.

        Returns:
            The current mode string, or None
        """
        return getattr(self._local, 'mode', None)

    def require_mode(self, mode: str):
        """
        Check that we're in the specified mode, set it if unset, error if different.

        This is a convenience method that combines get_mode checking with set_mode.

        Args:
            mode: The required mode

        Raises:
            RuntimeError: If a different mode was already set
        """
        self.set_mode(mode)

    def forbid_mode(self, mode: str, message: str | None = None):
        """
        Raise an error if the current mode matches the specified mode.

        Args:
            mode: The mode to forbid
            message: Optional custom error message. If None, uses the message from valid_modes.

        Raises:
            RuntimeError: If current mode matches the forbidden mode
        """
        current_mode = self.get_mode()
        if current_mode == mode:
            if message is None:
                message = self.valid_modes.get(
                    mode,
                    f"Cannot proceed: {self.name} is set to '{mode}', "
                    f"which is not allowed in this context."
                )
            raise RuntimeError(message)

    def reset(self):
        """
        Reset the mode for this context. Useful for testing or manual mode switching.
        """
        if hasattr(self._local, 'mode'):
            del self._local.mode

    def __repr__(self):
        mode = self.get_mode()
        return f"MutualExclusionManager(name='{self.name}', current_mode={mode!r})"
