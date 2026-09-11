##
## run: pytest test_uint32_to_rgba.py
##
import numpy as np
import pytest
from cubevis.toolbox.visplot.visibility_plot import _img_to_uint32


def _bytes(img32):
    h, w = img32.shape
    return img32.view(np.uint8).reshape(h, w, 4)


def test_pil_rgba_round_trips_unchanged():
    """PIL RGBA in → identical RGBA bytes out (plasma endpoints)."""
    pil = np.array([[[0x0D, 0x08, 0x87, 0xFF],
                     [0xF0, 0xF9, 0x21, 0xFF]]], dtype=np.uint8)
    got = _img_to_uint32(pil)
    assert got.dtype == np.uint32 and got.shape == (1, 2)
    np.testing.assert_array_equal(_bytes(got), pil)


def test_uint32_passthrough_is_contiguous():
    """A non-contiguous uint32 input still yields a viewable result."""
    src = np.arange(6, dtype=np.uint32).reshape(2, 3)
    got = _img_to_uint32(src[:, ::2])
    assert got.flags["C_CONTIGUOUS"]
    np.testing.assert_array_equal(got, src[:, ::2])


def test_rejects_float_rgba():
    with pytest.raises(ValueError):
        _img_to_uint32(np.zeros((2, 2, 4), dtype=np.float32))


@pytest.mark.skipif(
    pytest.importorskip("datashader", reason="datashader not installed") is None,
    reason="datashader not installed",
)
def test_datashader_emits_rgba_in_memory():
    """Red lands in byte 0 and blue in byte 2 — the export path depends on it."""
    import xarray as xr
    import datashader.transfer_functions as tf

    agg = xr.DataArray(np.array([[0.0, 1.0]]), dims=("y", "x"),
                       coords={"y": [0], "x": [0, 1]})
    img = tf.shade(agg, cmap=["#FF0000", "#0000FF"], how="linear",
                   span=[0.0, 1.0])
    b = _bytes(_img_to_uint32(img))
    assert b[0, 0, 0] == 255 and b[0, 0, 2] == 0      # low  → red
    assert b[0, 1, 0] == 0   and b[0, 1, 2] == 255    # high → blue
