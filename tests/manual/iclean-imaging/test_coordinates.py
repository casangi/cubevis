'''The coordinate description (cubevis.imaging._coordinates, design §2.5)
without CASA: built from a casatools-style coordsys record, its astropy WCS,
and its use by ImageCube and convert_masks' WCS helper.

    pytest tests/manual/iclean-imaging/test_coordinates.py
'''
import json
import math
import subprocess
import sys
import textwrap

import numpy as np
import pytest

from cubevis.imaging import ImageCube, from_casa_coordsys, astropy_wcs
from cubevis.imaging._coordinates import (beam_from_casa, celestial_axis_names, fits_ctype,
                                          linear_spectral_values, VERSION)

D2R = math.pi / 180.0


def record(system='J2000', projection='SIN', rot_deg=0.0, crpix=(64.0, 64.0)):
    '''a coordsys().torecord() of the shape casatools gives: direction in
    radians, pc transposed with respect to FITS PCi_j (column-major)'''
    a = math.radians(rot_deg)
    fits_pc = [[math.cos(a), math.sin(a)], [-math.sin(a), math.cos(a)]]       # PC1_1, PC1_2 / PC2_1, PC2_2
    return {
        'direction0': {'axes': ['Right Ascension', 'Declination'], 'system': system,
                       'projection': projection, 'projection_parameters': [0.0, 0.0],
                       'crval': [299.86875 * D2R, 40.6666667 * D2R], 'crpix': list(crpix),
                       'cdelt': [-12.0 / 3600 * D2R, 12.0 / 3600 * D2R], 'units': ['rad', 'rad'],
                       'pc': [[fits_pc[j][i] for j in range(2)] for i in range(2)],
                       'longpole': 180.0, 'latpole': 40.6666667},
        'stokes1': {'stokes': ['I', 'Q']},
        'spectral2': {'system': 'LSRK', 'unit': 'Hz', 'restfreq': 1.42e9, 'velType': 0,
                      'wcs': {'crval': 1.0e9, 'cdelt': 2.0e8, 'crpix': 0.0, 'ctype': 'FREQ', 'pc': 1.0}},
        'telescope': 'VLA', 'observer': 'someone',
        'obsdate': {'m0': {'unit': 'd', 'value': 60309.5}, 'refer': 'UTC', 'type': 'epoch'},
    }


def test_description_from_record():
    d = from_casa_coordsys(record(), [128, 128, 2, 5], beam={}, brightness_unit='Jy/beam')
    assert d['version'] == VERSION and d['shape'] == [128, 128, 2, 5] and d['pixel_origin'] == 0
    dr = d['direction']
    assert dr['frame'] == 'FK5' and dr['equinox'] == 2000.0 and dr['native_frame'] == 'J2000'
    assert dr['reference_value'] == pytest.approx([299.86875, 40.6666667])
    assert dr['increment'] == pytest.approx([-12.0 / 3600, 12.0 / 3600])
    assert dr['reference_pixel'] == [64.0, 64.0] and dr['pc'] == [[1.0, 0.0], [0.0, 1.0]]
    assert d['spectral']['values'] == pytest.approx([1.0e9, 1.2e9, 1.4e9, 1.6e9, 1.8e9])
    assert d['spectral']['frame'] == 'LSRK' and d['spectral']['velocity_convention'] == 'radio'
    assert d['stokes'] == ['I', 'Q'] and d['beam'] is None and d['brightness_unit'] == 'Jy/beam'
    assert d['extra']['telescope'] == 'VLA' and d['extra']['obsdate']['value'] == 60309.5
    json.dumps(d)                                                     # JSON-safe


def test_pc_is_fits_convention():
    d = from_casa_coordsys(record(rot_deg=30.0), [16, 16, 1, 1])
    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
    assert np.allclose(d['direction']['pc'], [[c, s], [-s, c]])     # PC1_2 = +sin, as FITS


@pytest.mark.parametrize('system,frame,equinox', [('J2000', 'FK5', 2000.0), ('ICRS', 'ICRS', None),
                                                   ('B1950', 'FK4', 1950.0), ('GALACTIC', 'GALACTIC', None),
                                                   ('AZEL', 'AZEL', None)])
def test_frames(system, frame, equinox):
    d = from_casa_coordsys(record(system=system), [8, 8, 1, 1])
    assert (d['direction']['frame'], d['direction']['equinox']) == (frame, equinox)


def test_explicit_spectral_values_win():
    d = from_casa_coordsys(record(), [8, 8, 1, 3], spectral_values=[1.0, 2.5, 7.0])
    assert d['spectral']['values'] == [1.0, 2.5, 7.0]
    assert linear_spectral_values(10.0, 2.0, 1.0, 3) == [8.0, 10.0, 12.0]


def test_beams():
    one = {'major': {'unit': 'arcmin', 'value': 0.5}, 'minor': {'unit': 'arcsec', 'value': 20.0},
           'positionangle': {'unit': 'rad', 'value': -0.5}}
    assert beam_from_casa(one) == pytest.approx({'major': 30.0, 'minor': 20.0, 'pa': -0.5 * 180 / math.pi})
    per = {'nChannels': 2, 'nStokes': 1, 'beams': {'*0': {'*0': one}, '*1': {'*0': one}}}
    assert len(beam_from_casa(per)['per_plane']) == 2 and len(beam_from_casa(per)['per_plane'][1]) == 1
    assert beam_from_casa({}) is None and beam_from_casa(None) is None


def test_ctypes():
    assert fits_ctype('RA', 'SIN') == 'RA---SIN' and fits_ctype('DEC', 'SIN') == 'DEC--SIN'
    assert fits_ctype('GLON', 'TAN') == 'GLON-TAN'
    assert celestial_axis_names('GALACTIC') == ('GLON', 'GLAT')
    assert celestial_axis_names('FK5') == ('RA', 'DEC')


def _fits_wcs(rot_deg=0.0):
    '''the same coordinates written as a FITS header (1-based CRPIX)'''
    from astropy.wcs import WCS
    a = math.radians(rot_deg)
    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---SIN', 'DEC--SIN']
    w.wcs.crval = [299.86875, 40.6666667]
    w.wcs.crpix = [65.0, 65.0]
    w.wcs.cdelt = [-12.0 / 3600, 12.0 / 3600]
    w.wcs.pc = [[math.cos(a), math.sin(a)], [-math.sin(a), math.cos(a)]]
    w.wcs.set()
    return w


@pytest.mark.parametrize('rot', [0.0, 30.0])
def test_astropy_wcs_matches_fits(rot):
    d = from_casa_coordsys(record(rot_deg=rot), [128, 128, 2, 5])
    pix = np.array([[0, 0], [64, 64], [100, 20], [127, 127], [33.5, 77.25]])
    mine = astropy_wcs(d).sub([1, 2]).wcs_pix2world(pix, 0)
    ref = _fits_wcs(rot).wcs_pix2world(pix, 0)
    assert np.abs(mine - ref).max() < 1e-10
    w4 = astropy_wcs(d)
    assert list(w4.wcs.ctype) == ['RA---SIN', 'DEC--SIN', 'STOKES', 'FREQ']
    assert w4.wcs.crval[2] == 1.0 and w4.wcs.cdelt[2] == 1.0                    # I, Q
    assert w4.wcs.crval[3] == pytest.approx(1.0e9) and w4.wcs.cdelt[3] == pytest.approx(2.0e8)


def test_galactic_astropy_wcs():
    d = from_casa_coordsys(record(system='GALACTIC'), [8, 8, 1, 1])
    assert list(astropy_wcs(d, naxis=2).wcs.ctype) == ['GLON-SIN', 'GLAT-SIN']


class _Store:
    image_path, mask_path = 'x.residual', None

    def __init__(self):
        self.calls = 0

    def shape(self):
        return [128, 128, 2, 5]

    def coordinates(self):
        self.calls += 1
        return from_casa_coordsys(record(), self.shape())


def test_cube_coordinates_once_and_coorddesc():
    st = _Store()
    cube = ImageCube(st)
    assert cube.coordinates() is cube.coordinates() and st.calls == 1
    cd = cube.coorddesc()
    assert cd['shape'] == (128, 128, 2, 5) and cd['coordinates'] is cube.coordinates()
    assert cd['csys'] is cube.coordinates()             # no casatools coordsys in this store


def test_convert_masks_wcs_helper():
    import cubevis.utils as U
    helper = getattr(U, '__get_wcs_object')
    d = from_casa_coordsys(record(), [128, 128, 2, 5])
    pix = np.array([[0, 0], [64, 64], [127, 3]])
    expect = _fits_wcs().wcs_pix2world(pix, 0)
    for cdesc in ({'csys': None, 'shape': (128, 128, 2, 5), 'coordinates': d},     # ImageCube.coorddesc()
                  {'csys': d, 'shape': (128, 128, 2, 5)},                          # a description as csys
                  {'csys': record(), 'shape': (128, 128, 2, 5)}):                  # a coordsys record
        assert np.abs(helper(cdesc).wcs_pix2world(pix, 0) - expect).max() < 1e-10


def test_no_astropy_until_used():
    code = textwrap.dedent('''
        import sys, cubevis.imaging
        from cubevis.imaging import from_casa_coordsys
        print("astropy" in sys.modules)
    ''')
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == 'False'
