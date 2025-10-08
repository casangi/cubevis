

To Be Fixed
===========

This section contains a list of issues to be fixed before the :code:`jupyter` branch
is merged to trunk:

*  replace this code with proper notebook detection code::

     ("""console.log("Running in jupyter notebook. Not closing window.")""" if True else
      """console.log("Running from script/terminal. Closing window.")
         window.close()"""
     ) +

*  *Next item here*

