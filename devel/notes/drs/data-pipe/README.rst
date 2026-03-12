
Background
==========
For communication between Python and JavaScript code in the browser, the original implementation
used ``DataPipe``. With this implementation, a websocket is created for each ``DataPipe``.
This encapsulates the websocket and allows objects built with ``DataPipe`` to be combined
into a single application. All of the websockets were then combined with::


  async with CMC( *( [ ctx for img in self._clean_targets.keys( ) for ctx in
                     [
                         self._clean_targets[img]['gui']['cube'].serve(self.__stop),
                     ]
                   ] + [ create_ws_server( self._pipe['control'].process_messages,
                                           self._pipe['control'].backend_ip,
                                           self._pipe['control'].backend_port ),
                         create_ws_server( self._clean['converge']['pipe'].process_messages,
                                           self._clean['converge']['pipe'].backend_ip,
                                           self._clean['converge']['pipe'].backend_port ) ]
                  ) ):
      self.__result_future = asyncio.Future( )
      yield self.__result_future

Unfortunately, this also results in multiple ports being used. While Colab provides capabilities
to forward ports, for example::

  from google.colab import output
  print(output.eval_js("google.colab.kernel.proxyPort(8000)"))

there were reportedly problems with proxying a number of ports. Worse after some work, it became
clear that the Colab's port proxy implements only limited functionality which does **not** allow
for upgrading the ``HTTP`` protocol to ``WS`` (websocket).

Multiplexing all of the ``DataPipe`` communicatinos over one websocket was an existing goal.
The fact that websocket communications are not possible in a Colab environment required an
implementation which would at least allow for websocket and *Colab Comms* communications. These
issues resulted in the reimplementation of the ``DataPipe`` communications. The new
classes are ``CommMgr`` which provides the communications conduit, which can be either
*websocket*, *Colab Comms* or *Jupyter Comms*, and creates a ``Comm`` object via the
``open`` member function for a communications channel dedicated to one sort of message
(*which is the functionality provided by DataPipe*).
