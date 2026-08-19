
# Debugging GUIs wrapped as a casatask

GUI wrappers for `casatask` functions create the GUI and then launch it supplying a synchronous
execution context like:

```python
    _app = VisibilityPlotter(
        ms = ms,
        ps = ps,
        backend = backend,
        ...
    )

    id = uuid4( )
    context = exe.Context( exe.Mode.SYNC )
    bokeh_ui, exec_task = _app( context, id )
    bokeh_ui.show( )
    return context.execute( exec_task, id )
```

This displays the GUI with a synchronous execution mode, blocking until a result is ready. To debug
the GUI interactively, This can be changed to:

```python
    _app = VisibilityPlotter(
        ms = ms,
        ps = ps,
        backend = backend,
        ...
    )

    id = uuid4( )
    context = exe.Context( exe.Mode.THREAD )
    global bokeh_ui, exec_task
    bokeh_ui, exec_task = _app( context, id )
    inject_to_cli("bokeh_app", _app)
    inject_to_cli("bokeh_ui", bokeh_ui)
    inject_to_cli("exec_task", exec_task)
    bokeh_ui.show( )
    return context.execute( exec_task, id )
```

This creates the same GUI but runs it as a thread which allows this function to return immediately.
This solves the primary problem with debugging this sort of wrapped GUI, but the problem remains
with how to expose the displayed GUI from the `ipython` cli. To do this, I use the `inject_to_cli`
function whose definition must be included in the `casatask` wrapper:

```python
    import sys
    def inject_to_cli(name, value):
        """
        Traverses up the call stack to find the interactive CLI/top-level frame
        and inserts a variable with the given name and value.
        """
        frame = sys._getframe()

        # Climb up until we hit the root frame (where f_back is None)
        # or until we find the global namespace representing the CLI interactive environment
        while frame.f_back is not None:
            # Check if the frame belongs to the interactive user environment
            if frame.f_globals.get('__name__') == '__main__':
                break
            frame = frame.f_back

        # Inject directly into that frame's global dictionary
        print( f'setting {name}' )
        frame.f_globals[name] = value
```

So for the example above `bokeh_app`, `bokeh_ui` and `exec_task` would all be exposed in the Python
CLI. This was tested with `ipython`.
