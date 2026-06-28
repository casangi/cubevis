import os
import tarfile
import urllib.request

# Define URL and target filenames
URL = "https://eso.org"
TAR_FILE = "group.uid___A001_X87c_X245.ec_jlgomez.e17a10-7-hi-oj287-3C279-fits.tgz"
EXPECTED_DIR = "ngEHT_E17A10.0.bin0000.source0000_split.ms"  # Target directory inside the archive

def setup_dataset():
    # 1. Check and download the archive
    if os.path.exists(TAR_FILE):
        print(f"Archive '{TAR_FILE}' already exists. Skipping download.")
    else:
        print(f"Downloading {TAR_FILE}...")
        try:
            urllib.request.urlretrieve(URL, TAR_FILE)
            print("Download complete.")
        except Exception as e:
            print(f"Error downloading file: {e}")
            return

    # 2. Check and unpack the archive
    if os.path.exists(EXPECTED_DIR):
        print(f"Unpacked directory '{EXPECTED_DIR}' already exists. Skipping extraction.")
    else:
        print(f"Extracting {TAR_FILE}...")
        try:
            with tarfile.open(TAR_FILE, "r:gz") as tar:
                tar.extractall()
            print("Extraction complete.")
        except Exception as e:
            print(f"Error extracting archive: {e}")

if __name__ == "__main__":
    setup_dataset()
