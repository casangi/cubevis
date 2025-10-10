

To Be Fixed
===========

This section contains a list of issues to be fixed before the :code:`jupyter` branch
is merged to trunk:

*  replace this code with proper notebook detection code in :code:`_cube.py`::

     ("""console.log("Running in jupyter notebook. Not closing window.")""" if True else
      """console.log("Running from script/terminal. Closing window.")
         window.close()"""
     ) +

   Interestingly, this only happens when the **Stop** button is pressed. Closing
   the tab is not caught. Should it be? It seems like there was a reason this was
   problematic.

*  Enter in debugging notes::

     print(f"Logger name: {logger.name}")
     print(f"Logger level: {logger.level}")
     print(f"Logger effective level: {logger.getEffectiveLevel()}")
     print(f"Logger handlers: {logger.handlers}")
     print(f"Logger propagate: {logger.propagate}")
     print(f"Logger parent: {logger.parent}")
     print(f"Root logger level: {logging.root.level}")
     print(f"Root logger handlers: {logging.root.handlers}")

