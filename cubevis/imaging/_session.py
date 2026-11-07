########################################################################
#
# Copyright (C) 2026
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
'''``LocalImagingSession``: the imaging layer in this process -- a
``Deconvolver`` (casatasks' ``gclean``, or the module ``iclean_backend``
names) and an ``ImageCube`` per field over a ``CasatoolsImageStore``.

The same class is what a remote worker will hold; "remote" only puts a
proxy in front of it.'''

from os.path import abspath, basename, join
from typing import Callable, Dict, List, Optional

from ._protocols import CycleResult
from ._image_cube import ImageCube
from ._casatools_store import CasatoolsImageStore


def resolve_deconvolver(iclean_backend: str = 'PROD'):
    '''The module providing ``gclean`` for ``iclean_backend``: "PROD" is
    casatasks' ``casatasks.private.imagerhelpers._gclean``; any other value
    NAME selects a module ``_gclean_NAME`` (an alternate or a stub backend).'''
    from ..utils import find_pkg, load_pkg
    if iclean_backend == 'PROD':
        mod_specs = find_pkg("casatasks.private.imagerhelpers._gclean")
    else:
        mod_specs = find_pkg(f"_gclean_{iclean_backend}")
    if not mod_specs:
        raise ImportError(f"Could not locate {iclean_backend} kind of iclean backend")
    return load_pkg(mod_specs[0])


class LocalImagingSession:
    '''An ``ImagingSession`` in this process.

    Parameters
    ----------
    deconvolver:
        an object with gclean's surface (``Deconvolver``)
    store_factory:
        ``store_factory(image_path, mask_path)`` makes the ``ImageStore``
        for one field; ``CasatoolsImageStore`` by default
    '''

    def __init__(self, deconvolver, *, store_factory: Callable = CasatoolsImageStore):
        self._dec = deconvolver
        self._store_factory = store_factory
        self._cubes: Dict[str, ImageCube] = {}
        self._products = None

    @classmethod
    def from_args(cls, args: dict, iclean_backend: str = 'PROD', **kwargs) -> 'LocalImagingSession':
        '''Make the deconvolver from iclean's (tclean's) arguments.'''
        module = resolve_deconvolver(iclean_backend)
        return cls(module.gclean(**args), **kwargs)

    @property
    def deconvolver(self):
        return self._dec

    # ---- the imager -------------------------------------------------------

    def _touch(self) -> None:
        for cube in self._cubes.values():
            cube.touch()

    def _result(self, values) -> CycleResult:
        '''gclean's six values as a ``CycleResult`` whose convergence record
        is keyed by field id.

        gclean keys the record by ``imagename`` as given, while its
        ``image_products()`` names a field by the image's base name
        (``basename(abspath(imagename))``): with an imagename that has a
        directory -- an absolute path, as a remote kernel's paths usually are
        -- the GUI found no field for the record and interactive clean failed
        at start-up (before this layer too; notes §67).'''
        result = CycleResult(*values)
        conv = result.convergence
        if isinstance(conv, dict):
            ids = {f['id'] for f in self.fields()}
            if any(k not in ids for k in conv):
                rekeyed = {}
                for k, v in conv.items():
                    base = basename(abspath(k)) if isinstance(k, str) else k
                    rekeyed[k if k in ids or base not in ids else base] = v
                result = result._replace(convergence=rekeyed)
        return result

    def start(self) -> CycleResult:
        '''The first step: the dirty image (``next(gclean)``).'''
        result = self._result(next(self._dec))
        self._touch()
        return result

    def update(self, params: dict):
        return self._dec.update(params)

    def run_cycle(self) -> CycleResult:
        '''One major cycle and its minor cycles; blocks (minutes).'''
        result = self._result(next(self._dec))
        self._touch()
        return result

    async def arun_cycle(self) -> CycleResult:
        '''``run_cycle`` without blocking the event loop (gclean runs it in
        a thread: ``gclean.__anext__``).'''
        result = self._result(await self._dec.__anext__())
        self._touch()
        return result

    def has_next(self) -> bool:
        return self._dec.has_next()

    def log(self, history: bool = False) -> List[str]:
        return self._dec._log(history)

    def restore(self) -> dict:
        return self._dec.restore()

    def stop_descriptions(self) -> Dict[int, str]:
        '''Minor-cycle stop code -> description, for the convergence plots.'''
        own = getattr(self._dec, 'stop_descriptions', None)
        if callable(own):
            return dict(own())
        # pylint: disable=no-name-in-module
        from casatasks.private.imagerhelpers.imager_return_dict import ImagingDict
        return dict(ImagingDict.get_summaryminor_stopdesc())

    # ---- the images -------------------------------------------------------

    def image_products(self) -> List[dict]:
        if self._products is None:
            self._products = list(self._dec.image_products())
        return self._products

    def fields(self) -> List[dict]:
        '''One dict per field: ``id`` (gclean's ``name``), and, informational
        only, the paths on this host (``imagepath``, ``imagename``,
        ``residual``, ``mask``).'''
        out = []
        for prod in self.image_products():
            out.append(dict(id=prod['name'], imagename=prod.get('imagename'),
                            imagepath=prod['imagepath'],
                            residual=join(prod['imagepath'], prod['residualname']),
                            mask=join(prod['imagepath'], prod['maskname'])))
        return out

    def cube(self, field: str) -> ImageCube:
        '''The field's ``ImageCube`` (made once; the residual is opened on
        first use, so this can be called before ``start``).'''
        if field not in self._cubes:
            info = {f['id']: f for f in self.fields()}
            if field not in info:
                raise KeyError(f'no field {field!r}; fields are {sorted(info)}')
            store = self._store_factory(info[field]['residual'], info[field]['mask'])
            self._cubes[field] = ImageCube(store, track='generation')
        return self._cubes[field]

    def close(self) -> None:
        for cube in self._cubes.values():
            try:
                cube.close()
            except Exception:
                pass
        self._cubes.clear()


def as_imaging_session(obj) -> 'LocalImagingSession':
    '''``obj`` if it is already an imaging session, else a gclean-like
    deconvolver wrapped in a ``LocalImagingSession`` (callers that still
    pass a gclean, e.g. ``InteractiveCleanUI(gclean, args)``).'''
    if hasattr(obj, 'fields') and hasattr(obj, 'cube') and hasattr(obj, 'start'):
        return obj
    return LocalImagingSession(obj)


def open_image_cube(image_path: str, mask_path: Optional[str] = None) -> ImageCube:
    '''An ``ImageCube`` over a CASA image on this computer (createmask,
    createregion, a standalone ``ImagePipe``).'''
    return ImageCube(CasatoolsImageStore(image_path, mask_path), track='ctime')
