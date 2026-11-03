"""Make a synthetic MSv2 with xradio's test generator (5 antennas, 8 channels).
Usage: python gen_scaling_ms.py out.ms ROWS_PER_DDI N_SPW   (assessment §10.3)"""
import copy, sys, warnings; warnings.filterwarnings("ignore")
from xradio.testing.measurement_set.msv2_io import gen_test_ms, default_ms_descr
name, nrows, nspw = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
d = copy.deepcopy(default_ms_descr)
d["nrows_per_ddi"] = nrows; d["nchans"] = 8
d["ANTENNA"] = {str(i): i for i in range(5)}
d["SPECTRAL_WINDOW"] = {str(i): i for i in range(nspw)}
gen_test_ms(name, descr=d, vlbi_tables=False)
