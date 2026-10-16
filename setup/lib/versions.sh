# What cubevis-setup installs. One place to change for a new release.
# (Sourced by cubevis-setup; plain variable assignments only.)

# cubevis release installed on both sides when the checkout is not at a
# release tag (a checkout at tag vX.Y.Z installs X.Y.Z).
CV_RELEASE_DEFAULT="1.0.96"

# The conda environment: same packages on your computer and on each
# remote host (conda-forge only).
CV_ENV_DEFAULT="cubevis"
CV_CONDA_PKGS="python=3.12 pip ipykernel jupyter_client scipy regions zarr>=3 datashader certifi"

# pip, after the conda packages: cubevis on both sides; sshpyk only on
# your computer (it starts the remote kernels).
CV_PIP_COMMON='cubevis[visplot]==@RELEASE@'
CV_PIP_LOCAL="sshpyk"

# Miniforge, when there is no conda yet.
CV_MINIFORGE_URL="https://github.com/conda-forge/miniforge/releases/latest/download"

# Test data for the last check (a small ALMA measurement set).
CV_TEST_DATA_URL="https://casa.nrao.edu/download/devel/casavis/data/sis14_twhya_calibrated_flagged.ms.tar.gz"
CV_TEST_MS="sis14_twhya_calibrated_flagged.ms"
CV_TEST_FIELD="3c279"
