"""Simulate a small VLA MS like refim_point_withline.ms: a point source with
a line, five channels from 1.0 GHz in 0.2 GHz steps, plus a weaker second
source, for iclean tests in an environment that cannot reach casa.nrao.edu."""
import os, sys, shutil
import numpy as np
from casatools import simulator, componentlist, measures, quanta, ctsys, table

out = sys.argv[1] if len(sys.argv) > 1 else 'sim_point_withline.ms'
cl_path = out + '.cl'
for p in (out, cl_path):
    if os.path.exists(p):
        shutil.rmtree(p)

me, qa = measures(), quanta()
ra, dec = '19h59m28.5s', '+40d40m00s'
nchan, f0, df = 5, 1.0e9, 0.2e9

cl = componentlist()
cl.addcomponent(dir=f'J2000 {ra} {dec}', flux=1.0, fluxunit='Jy', freq='1.5GHz',
                shape='point', spectrumtype='constant')
# the "line": a component whose flux is set per channel
cl.addcomponent(dir='J2000 19h59m24.0s +40d42m00s', flux=0.5, fluxunit='Jy',
                freq='1.5GHz', shape='point', spectrumtype='constant')
cl.setspectrum(which=1, type='tabular',
               tabularfreqs=[f0 + i * df for i in range(nchan)],
               tabularflux=[0.0, 0.2, 0.8, 0.2, 0.0])
cl.rename(cl_path)
cl.close()

cfg = os.path.join(ctsys.resolve('alma/simmos'), 'vla.d.cfg')
tb = table()
xx, yy, zz, diam, names = [], [], [], [], []
for line in open(cfg):
    if line.startswith('#') or not line.strip():
        continue
    parts = line.split()
    xx.append(float(parts[0])); yy.append(float(parts[1])); zz.append(float(parts[2]))
    diam.append(float(parts[3])); names.append(parts[4])

sm = simulator()
sm.open(out)
sm.setconfig(telescopename='VLA', x=xx, y=yy, z=zz, dishdiameter=diam,
             mount=['alt-az'], antname=names, coordsystem='local',
             referencelocation=me.observatory('VLA'))
sm.setspwindow(spwname='LBand', freq='1.0GHz', deltafreq='0.2GHz',
               freqresolution='0.2GHz', nchannels=nchan, stokes='RR LL')
sm.setfeed(mode='perfect R L')
sm.setfield(sourcename='src', sourcedirection=me.direction('J2000', ra, dec))
sm.setlimits(shadowlimit=0.01, elevationlimit='10deg')
sm.setauto(autocorrwt=0.0)
sm.settimes(integrationtime='60s', usehourangle=True,
            referencetime=me.epoch('UTC', '2024/01/01/00:00:00'))
sm.observe('src', 'LBand', starttime='-2h', stoptime='2h')
sm.predict(complist=cl_path)
sm.setnoise(mode='simplenoise', simplenoise='0.05Jy')
sm.corrupt()
sm.close()
print('wrote', out)
