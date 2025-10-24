

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

Development Notes
=================


* While attempting to fix Jupyter Lab focus problems where typing numbers in
  text entries inserts hash signs into the cell causing the GUI to disappear
  this code was developed to inject keypress management as part of the
  :code:`_repr_html_` result::

       focus_management = """
       <script>
       (function() {
           setTimeout(function() {
               const showableRoot = document.querySelector('.bk-cubevis-bokeh-models-_showable-Showable');
               if (!showableRoot) {
                   console.warn('Showable root not found');
                   return;
               }

               console.log('Installing Bokeh focus management...');

               // Only install if not already installed
               if (window._bokehFocusManagementInstalled) {
                   console.log('Focus management already installed');
                   return;
               }
               window._bokehFocusManagementInstalled = true;
        
               function disableJupyterKeyboard() {
                   if (window.Jupyter && Jupyter.notebook && Jupyter.notebook.keyboard_manager.enabled) {
                       Jupyter.notebook.keyboard_manager.disable();
                       console.log('✓ Disabled Jupyter Notebook keyboard shortcuts');
                   }
               }
        
               function enableJupyterKeyboard() {
                   if (window.Jupyter && Jupyter.notebook && !Jupyter.notebook.keyboard_manager.enabled) {
                       Jupyter.notebook.keyboard_manager.enable();
                       console.log('✓ Re-enabled Jupyter Notebook keyboard shortcuts');
                   }
               }
        
               // Use focusin/focusout for reliable keyboard management
               showableRoot.addEventListener('focusin', function() {
                   disableJupyterKeyboard();
               });
        
               showableRoot.addEventListener('focusout', function() {
                   // Use a small delay to avoid race conditions when switching between inputs
                   setTimeout(enableJupyterKeyboard, 50);
               });

               // Use a single, robust keydown listener on the root element
               showableRoot.addEventListener('keydown', function(e) {
                   // Check if the event's target is within our widget
                   if (showableRoot.contains(e.target)) {
                       const activeElement = document.activeElement;

                       // Ensure the currently focused element is also within our widget
                       if (showableRoot.contains(activeElement)) {
                           // Keys to allow to propagate within the widget
                           const keysToAllow = [
                               'Shift', 'Control', 'Alt', 'Meta',
                               'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
                               'Backspace', 'Delete', 'Enter', 'Tab',
                           ];

                           // Check if the pressed key is not in the allow list
                           if (!keysToAllow.includes(e.key)) {
                               e.stopPropagation();
                               console.log('🛑 BLOCKED keydown at root:', e.key);
                           } else {
                               console.log('✅ ALLOWED keydown at root:', e.key);
                           }
                       }
                   }
               }, true); // Use the capturing phase to ensure this runs first

               console.log('✓ Bokeh focus management installed (focus-based)');
           }, 1000);
       })();
       </script>
       """
       script, div = components(self)
       if start_backend: self._start_backend( )
            self._notebook_rendering = f'''
            {script}
            {div}
            {focus_management}
       '''
       return self._notebook_rendering

  However, it seems that only the :code:`keydown` handler was really effective because the
  :code:`focusin` and :code:`focusout` events were **seldom** triggered.
