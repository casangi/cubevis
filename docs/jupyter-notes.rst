

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

*  *Next item here*

