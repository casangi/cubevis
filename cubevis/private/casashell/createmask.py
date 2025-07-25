########################################################################
#
# Copyright (C) 2024
# Associated Universities, Inc. Washington DC, USA.
#
# This script is free software; you can redistribute it and/or modify it
# under the terms of the GNU Library General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This library is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Library General Public
# License for more details.
#
# You should have received a copy of the GNU Library General Public License
# along with this library; if not, write to the Free Software Foundation,
# Inc., 675 Massachusetts Ave, Cambridge, MA 02139, USA.
#
# Correspondence concerning AIPS++ should be adressed as follows:
#        Internet email: casa-feedback@nrao.edu.
#        Postal address: AIPS++ Project Office
#                        National Radio Astronomy Observatory
#                        520 Edgemont Road
#                        Charlottesville, VA 22903-2475 USA
#
########################################################################
##################### generated by xml-casa (v2) from createmask.xml
##################### 26fdebc4cbd4e58e368f393b9b7ccde7 ##############################
from __future__ import absolute_import
from casashell.private.stack_manip import find_local as __sf__
from casashell.private.stack_manip import find_frame as _find_frame
from casatools.typecheck import validator as _pc
from casatools.coercetype import coerce as _coerce
from casatools.errors import create_error_string
from casatasks.private.task_logging import start_log as _start_log
from casatasks.private.task_logging import end_log as _end_log
from cubevis.private.apps import InteractiveClean
from collections import OrderedDict
import numpy
import sys
import os

import shutil

def static_var(varname, value):
    def decorate(func):
        setattr(func, varname, value)
        return func
    return decorate


def _createmask_t( *args, **kwargs ):
    ic = InteractiveClean( *args, **kwargs )
    return ic( )


class _createmask:
    """
    createmask ---- Create mask cubes interactively with a GUI

    Create a mask cube for one or more images using a GUI.
    This task is based upon the same tools that are used to build
    the interactive clean application so the same command keys are
    used for both applications.


    --------- parameter descriptions ---------------------------------------------

    image
    mask
    create

    --------- examples -----------------------------------------------------------




    """

    _info_group_ = """imaging"""
    _info_desc_ = """Create mask cubes interactively with a GUI"""

    __schema = { 'image': {'anyof': [{'type': 'cReqPath', 'coerce': _coerce.expand_path}, {'type': 'cReqPathVec', 'coerce': [_coerce.to_list,_coerce.expand_pathvec]}]}, 'mask': {'anyof': [{'type': 'cPath', 'coerce': _coerce.expand_path}, {'type': 'cPathVec', 'coerce': [_coerce.to_list,_coerce.expand_pathvec]}]}, 'create': {'type': 'cBool'},  }

    def __init__(self):
        self.__stdout = None
        self.__stderr = None
        self.__root_frame_ = None

    def __globals_(self):
        if self.__root_frame_ is None:
            self.__root_frame_ = _find_frame( )
            assert self.__root_frame_ is not None, "could not find CASAshell global frame"
        return self.__root_frame_

    def __to_string_(self,value):
        if type(value) is str:
            return "'%s'" % value
        else:
            return str(value)

    def __validate_(self,doc,schema):
        return _pc.validate(doc,schema)

    def __do_inp_output(self,param_prefix,description_str,formatting_chars):
        out = self.__stdout or sys.stdout
        description = description_str.split( )
        prefix_width = 23 + 23 + 4
        output = [ ]
        addon = ''
        first_addon = True
        if len(description) == 0:
            out.write(param_prefix + " #\n")
            return
        while len(description) > 0:
            ## starting a new line.....................................................................
            if len(output) == 0:
                ## for first line add parameter information............................................
                if len(param_prefix)-formatting_chars > prefix_width - 1:
                    output.append(param_prefix)
                    continue
                addon = param_prefix + ' #'
                first_addon = True
                addon_formatting = formatting_chars
            else:
                ## for subsequent lines space over prefix width........................................
                addon = (' ' * prefix_width) + '#'
                first_addon = False
                addon_formatting = 0
            ## if first word of description puts us over the screen width, bail........................
            if len(addon + description[0]) - addon_formatting + 1 > self.term_width:
                ## if we're doing the first line make sure it's output.................................
                if first_addon: output.append(addon)
                break
            while len(description) > 0:
                ## if the next description word puts us over break for the next line...................
                if len(addon + description[0]) - addon_formatting + 1 > self.term_width: break
                addon = addon + ' ' + description[0]
                description.pop(0)
            output.append(addon)
        out.write('\n'.join(output) + '\n')

    #--------- go functions -----------------------------------------------------------
    def __image_dflt( self, glb ):
        return [ ]

    def __image( self, glb ):
        if 'image' in glb: return glb['image']
        return [ ]

    def __image_inp(self):
        def xml_default( ):
            ## play the crazy subparameter shell game
            dflt = self.__image_dflt( self.__globals_( ) )
            if dflt is not None: return dflt
            return [ ]
        description = 'Image(s) for which a mask cube will be created or updated'
        value = self.__image( self.__globals_( ) )
        (pre,post) = (('','') if value == xml_default( ) else ('\x1B[34m','\x1B[0m')) if self.__validate_({'image': value},{'image': self.__schema['image']}) else ('\x1B[91m','\x1B[0m')
        self.__do_inp_output('%-6.6s = %s%-23s%s' % ('image',pre,self.__to_string_(value),post),description,0+len(pre)+len(post))

    def __mask_dflt( self, glb ):
        return [  ]

    def __mask( self, glb ):
        if 'mask' in glb: return glb['mask']
        return [  ]

    def __mask_inp(self):
        def xml_default( ):
            ## play the crazy subparameter shell game
            dflt = self.__mask_dflt( self.__globals_( ) )
            if dflt is not None: return dflt
            return [  ]
        description = 'Mask cubes(s) to be updated or created'
        value = self.__mask( self.__globals_( ) )
        (pre,post) = (('','') if value == xml_default( ) else ('\x1B[34m','\x1B[0m')) if self.__validate_({'mask': value},{'mask': self.__schema['mask']}) else ('\x1B[91m','\x1B[0m')
        self.__do_inp_output('%-6.6s = %s%-23s%s' % ('mask',pre,self.__to_string_(value),post),description,0+len(pre)+len(post))

    def __create_dflt( self, glb ):
        return True

    def __create( self, glb ):
        if 'create' in glb: return glb['create']
        return True

    def __create_inp(self):
        def xml_default( ):
            ## play the crazy subparameter shell game
            dflt = self.__create_dflt( self.__globals_( ) )
            if dflt is not None: return dflt
            return True
        description = 'If equal to True, mask cubes are created if they do not exist'
        value = self.__create( self.__globals_( ) )
        (pre,post) = (('','') if value == xml_default( ) else ('\x1B[34m','\x1B[0m')) if self.__validate_({'create': value},{'create': self.__schema['create']}) else ('\x1B[91m','\x1B[0m')
        self.__do_inp_output('%-6.6s = %s%-23s%s' % ('create',pre,self.__to_string_(value),post),description,0+len(pre)+len(post))


    #--------- global default implementation-------------------------------------------
    @static_var('state', __sf__('casa_inp_go_state'))
    def set_global_defaults(self):
        self.set_global_defaults.state['last'] = self
        glb = self.__globals_( )
        if 'image' in glb: del glb['image']
        if 'mask' in glb: del glb['mask']
        if 'create' in glb: del glb['create']

    #--------- inp function -----------------------------------------------------------
    def inp(self):
        print("# createmask -- %s" % self._info_desc_)
        self.term_width, self.term_height = shutil.get_terminal_size(fallback=(80, 24))
        self.__image_inp( )
        self.__mask_inp( )
        self.__create_inp( )

    #--------- tget function ----------------------------------------------------------
    @static_var('state', __sf__('casa_inp_go_state'))
    def tget(self,savefile=None):
        from runpy import run_path
        filename = savefile
        if filename is None:
            filename = "createmask.last" if os.path.isfile("createmask.last") else "createmask.saved"
        if os.path.isfile(filename):
            glob = _find_frame( )
            newglob = run_path( filename, init_globals={ } )
            for i in newglob:
                glob[i] = newglob[i]
            self.tget.state['last'] = self
        else:
            print("could not find last file: %s\nsetting defaults instead..." % filename)
            self.set_global_defaults( )

    #--------- tput function ----------------------------------------------------------
    def tput(self,outfile=None):
        def noobj(s):
           if s.startswith('<') and s.endswith('>'):
               return "None"
           else:
               return s

        _postfile = outfile if outfile is not None else os.path.realpath('createmask.last')

        _invocation_parameters = OrderedDict( )
        _invocation_parameters['image'] = self.__image( self.__globals_( ) )
        _invocation_parameters['mask'] = self.__mask( self.__globals_( ) )
        _invocation_parameters['create'] = self.__create( self.__globals_( ) )

        try:
            with open(_postfile,'w') as _f:
                for _i in _invocation_parameters:
                    _f.write("%-20s = %s\n" % (_i,noobj(repr(_invocation_parameters[_i]))))
                _f.write("#createmask( ")
                count = 0
                for _i in _invocation_parameters:
                    _f.write("%s=%s" % (_i,noobj(repr(_invocation_parameters[_i]))))
                    count += 1
                    if count < len(_invocation_parameters): _f.write(",")
                _f.write(" )\n")
        except: return False
        return True

    def __call__( self, image=None, mask=None, create=None ):
        def noobj(s):
           if s.startswith('<') and s.endswith('>'):
               return "None"
           else:
               return s
        _prefile = os.path.realpath('createmask.pre')
        _postfile = os.path.realpath('createmask.last')
        task_result = None
        _arguments = [image,mask,create,]
        _invocation_parameters = OrderedDict( )
        if any(map(lambda x: x is not None,_arguments)):
            # invoke python style
            # set the non sub-parameters that are not None
            local_global = { }
            if image is not None: local_global['image'] = image
            if mask is not None: local_global['mask'] = mask
            if create is not None: local_global['create'] = create

            # the invocation parameters for the non-subparameters can now be set - this picks up those defaults
            _invocation_parameters['image'] = self.__image( local_global )
            _invocation_parameters['mask'] = self.__mask( local_global )
            _invocation_parameters['create'] = self.__create( local_global )

            # the sub-parameters can then be set. Use the supplied value if not None, else the function, which gets the appropriate default

        else:
            # invoke with inp/go semantics
            _invocation_parameters['image'] = self.__image( self.__globals_( ) )
            _invocation_parameters['mask'] = self.__mask( self.__globals_( ) )
            _invocation_parameters['create'] = self.__create( self.__globals_( ) )
        try:
            with open(_prefile,'w') as _f:
                for _i in _invocation_parameters:
                    _f.write("%-20s = %s\n" % (_i,noobj(repr(_invocation_parameters[_i]))))
                _f.write("#createmask( ")
                count = 0
                for _i in _invocation_parameters:
                    _f.write("%s=%s" % (_i,noobj(repr(_invocation_parameters[_i]))))
                    count += 1
                    if count < len(_invocation_parameters): _f.write(",")
                _f.write(" )\n")
        except: pass
        try:
            _logging_state_ = None
            assert _pc.validate(_invocation_parameters,self.__schema), create_error_string(_pc.errors)
            _logging_state_ = _start_log( 'createmask', [ 'image=' + repr(_pc.document['image']), 'mask=' + repr(_pc.document['mask']), 'create=' + repr(_pc.document['create']), ] )
            task_result = _createmask_t( image=_pc.document['image'],mask=_pc.document['mask'],create=_pc.document['create'], )
        except Exception as e:
            from traceback import format_exc
            from casatasks import casalog
            casalog.origin('createmask')
            casalog.post("Exception Reported: Error in createmask: %s" % str(e),'SEVERE')
            casalog.post(format_exc( ))
            raise #exception is now raised
            #task_result = False
        finally:
            try:
                os.rename(_prefile,_postfile)
            except: pass
            if _logging_state_:
                task_result = _end_log( _logging_state_, 'createmask', task_result )

        #Added if _createmask_t returns False and does not raise an exception.
        if task_result is False:
            raise

        return task_result #Still needed

createmask = _createmask( )
