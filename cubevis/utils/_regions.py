
def polygon_indexes( xs, ys, shape ):
    '''
    Returns indexes for a 2D array of the given ``shape`` which
    lie within the polygon specified by ``xs`` and ``ys``.

    Parameters
    ----------
    xs: list of numbers
        the X coordinates for the vertices of the polygon
    ys: list of numbers
        the Y coordinates for the vertices of the polygon
    shape: ( int, int )
        the shape of the plane in which the polygon is found

    Returns
    -------
    generator of tuples:
        the stream of tuples that is returned will be the indexes
        of the elements which lie within the polygon
    '''
    ### the selection is cubevis.imaging._masks.polygon_pixels' (vectorized; the same pixels)
    from ..imaging._masks import polygon_pixels
    ix, iy = polygon_pixels( xs, ys, shape )
    return zip( map(int, ix), map(int, iy) )
