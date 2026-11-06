"""Drive interactive clean headlessly through the handlers the browser uses
(ImagePipe's channel/spectrum/colormap messages, CubeMask's mask edits and
spectrum fetch, the clean buttons, the convergence pipe) and pickle every
reply, the mask cube after each edit, statistics and the cycles' results --
so that a change can be compared with the code before it:

    mkdir a && cd a && BROWSER=true python ../record_iclean_handlers.py MS ../before.pkl
    # change the code
    mkdir b && cd b && BROWSER=true python ../record_iclean_handlers.py MS ../after.pkl
    python compare_records.py before.pkl after.pkl

Run each in an empty directory (the images are written there).  MS:
refim_point_withline.ms, or the one make_sim_ms.py writes.  BROWSER=true
only where no browser is installed (cubevis asks webbrowser for one).
Needs casatools and casatasks.
"""
import sys, os, asyncio, pickle, copy
import numpy as np

ms, out = sys.argv[1], sys.argv[2]
from cubevis.private.apps import InteractiveClean as IClean      # the public name (works before and after design §6)
from casatools import image as _ia

ARGS = dict(vis=ms, imagename='bl', imsize=128, cell='12.0arcsec', specmode='cube',
            interpolation='nearest', nchan=5, start='1.0GHz', width='0.2GHz', pblimit=-1e-05,
            deconvolver='hogbom', threshold='0.001Jy', cyclefactor=3, niter=60, cycleniter=10)

rec = {}
def put(k, v):
    rec[k] = copy.deepcopy(v)

def mask_cube(name='bl.mask'):
    ia = _ia(); ia.open(name); v = ia.getchunk(); ia.close(); return v

def handlers(comm_mgr):
    out = {}
    for cid, hs in comm_mgr._handlers.items():
        for mid, h in hs.items():
            out[mid] = h
    return out

async def call(h, msg):
    import inspect
    r = h(msg, context=None) if 'context' in inspect.signature(h).parameters else h(msg)
    if asyncio.iscoroutine(r):
        r = await r
    return r

async def main():
    ic = IClean(**ARGS)
    ui = ic._ui
    ui._setup()
    ui._build_bokeh()
    put('last_success_initial', ui._clean['last-success'])
    targets = ui._clean_targets
    put('fields', list(targets.keys()))
    cube = targets['bl']['gui']['cube']
    pipe = cube._pipe['image']
    put('shape', list(cube.shape()))
    hdr, hdr_str = cube.fits_header()
    put('fits_header', hdr); put('fits_header_str', hdr_str)
    if hasattr(pipe, 'fits_header_json'):                     # until 2026-11-06
        put('fits_header_json', pipe.fits_header_json)
    if hasattr(pipe, 'coords_json'):                          # the coordinate description, since
        put('coords_json', pipe.coords_json)                  # 2026-11-06
    cd = cube.coorddesc()
    put('coorddesc_shape', cd['shape'])
    csys = cd['csys']
    put('coorddesc_csys', csys if isinstance(csys, dict) else csys.torecord())
    put('stokes_labels', list(pipe.stokes_labels()))
    put('have_mask0', pipe.have_mask0())

    H = handlers(ui._comm_mgr)
    img = lambda m: call(pipe._image_message_handler, m)

    for c in range(5):
        put(f'channel_{c}', await img({'action': 'channel', 'index': [0, c], 'id': f'c{c}'}))
    put('spectrum', await img({'action': 'spectrum', 'index': [64, 64, 0], 'id': 's'}))
    put('adjust', await img({'action': 'adjust-colormap', 'bounds': [[-0.01], [0.2]],
                              'transfer': {'scaling': 'log', 'args': {'alpha': 1000}}, 'id': 'a'}))
    put('channel_2_log', await img({'action': 'channel', 'index': [0, 2], 'id': 'c2l'}))
    put('adjust_sqrt', await img({'action': 'adjust-colormap', 'bounds': [[], []],
                                   'transfer': {'scaling': 'sqrt', 'args': {}}, 'id': 'a2'}))
    put('channel_2_sqrt', await img({'action': 'channel', 'index': [0, 2], 'id': 'c2s'}))
    put('adjust_reset', await img({'action': 'adjust-colormap', 'bounds': 'reset', 'id': 'r'}))
    put('channel_2_reset', await img({'action': 'channel', 'index': [0, 2], 'id': 'c2r'}))
    put('pixel_value', pipe.pixel_value([0, 2], [64, 64]))
    put('stats_0_1', pipe.statistics([0, 1]))
    put('histogram_0_3', pipe.histogram([0, 3]))
    put('mask_value', pipe.mask_value([0, 0], [64, 64]))

    mod = H[cube._ids['mask-mod']]
    sq = {'xs': [54, 74, 74, 54], 'ys': [54, 54, 74, 74]}
    sq2 = {'xs': [10, 30, 30, 10], 'ys': [90, 90, 110, 110]}
    steps = [
        ('add_chan', {'action': 'addition', 'scope': 'chan', 'value': dict(sq, chan=[0, 2])}),
        ('add_cube', {'action': 'addition', 'scope': 'cube', 'value': dict(sq2, chan=[0, 2])}),
        ('sub_chan', {'action': 'subtract', 'scope': 'chan', 'value': dict(sq2, chan=[0, 1])}),
        ('src_add_chan', {'action': 'addition', 'scope': 'chan', 'value': {'src': [0, 2], 'chan': [0, 4]}}),
        ('src_sub_cube', {'action': 'subtract', 'scope': 'cube', 'value': {'src': [0, 1], 'chan': [0, 3]}}),
        ('not_chan', {'action': 'not', 'scope': 'chan', 'value': {'chan': [0, 0]}}),
        ('not_chan_again', {'action': 'not', 'scope': 'chan', 'value': {'chan': [0, 0]}}),
        ('delete_chan', {'action': 'delete', 'scope': 'chan', 'value': {'chan': [0, 4]}}),
        ('add_cube_center', {'action': 'addition', 'scope': 'cube', 'value': dict(sq, chan=[0, 0])}),
    ]
    for name, m in steps:
        put(f'mod_{name}', await call(mod, m))
        put(f'maskcube_{name}', mask_cube())
    put('channel_2_after_mask', await img({'action': 'channel', 'index': [0, 2], 'id': 'cm'}))
    pipe.statistics_config(use_mask=True)
    put('stats_masked_0_2', pipe.statistics([0, 2]))
    pipe.statistics_config(use_mask=False)

    fs = H[cube._ids['fetch-spectrum']]
    put('fetch_spectrum', await call(fs, {'action': 'spectrum', 'value': {'chan': [0, 2], 'index': [64, 64]}}))

    clean = H[ui._clean_ids['continue']]
    it = {'niter': '60', 'cycleniter': '10', 'nmajor': '-1', 'threshold': '0.001Jy',
          'cyclefactor': '3', 'gain': '0.1', 'nsigma': '0.0'}
    for i in range(2):
        r = await call(clean, {'action': 'next', 'value': {'iteration': it, 'automask': {},
                                                            'masks': {}, 'breadcrumbs': {}}})
        put(f'clean_{i}', r)
        put(f'channel_2_after_clean_{i}', await img({'action': 'channel', 'index': [0, 2], 'id': f'cc{i}'}))
        put(f'stats_after_clean_{i}', pipe.statistics([0, 2]))
    put('status', await call(clean, {'action': 'status', 'value': {}}))
    conv = H[ui._clean['converge']['id']]
    put('convergence_retrieve', await call(conv, {'action': 'retrieve'}))
    put('log_history', ui._clean['cmds'] if 'cmds' in ui._clean else None)
    return ui

ui = asyncio.run(main())
with open(out, 'wb') as f:
    pickle.dump(rec, f)
print('wrote', out, len(rec), 'entries')
os._exit(0)
