#!/usr/bin/env python3
"""
Capture JavaScript library version information for inclusion in the Python wheel.
This runs during the Python build process to embed version info from casalib, cubevisjs, etc.
"""
import json
from pathlib import Path

def get_js_library_version(library_path):
    """Get version from a JavaScript library's package.json"""
    package_json = library_path / "package.json"
    
    if not package_json.exists():
        print(f"Warning: package.json not found at {package_json}")
        return "unknown"
    
    try:
        with open(package_json, 'r') as f:
            package_data = json.load(f)
            version = package_data.get("version", "unknown")
            library_name = package_data.get("name", library_path.name)
            print(f"Found {library_name} version: {version}")
            return version
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Warning: Could not read {package_json}: {e}")
        return "unknown"

def capture_js_versions():
    """Capture all JavaScript library versions and write them to the Python package"""
    script_dir = Path(__file__).parent
    root_dir = script_dir.parent
    cubevis_dir = root_dir / "cubevis"
    
    if not cubevis_dir.exists():
        print(f"Error: cubevis directory not found at {cubevis_dir}")
        return
    
    # Define the JavaScript libraries to capture
    js_libraries = {
        "casalib": root_dir / "casalib",
        "cubevisjs": root_dir / "cubevisjs",
        # Add more libraries here as needed
    }
    
    # Capture versions
    versions = {}
    for lib_name, lib_path in js_libraries.items():
        versions[lib_name] = get_js_library_version(lib_path)
    
    # Generate Python file with all versions
    version_file = cubevis_dir / "_js_versions.py"
    version_content = '''"""
JavaScript library version information captured at build time.
This file is generated automatically - do not edit manually.
"""

'''
    
    # Add individual version constants
    for lib_name, version in versions.items():
        const_name = f"{lib_name.upper()}_VERSION"
        version_content += f'{const_name} = "{version}"\n'
    
    # Add a dictionary with all versions
    version_content += '\n# All JavaScript library versions\n'
    version_content += 'JS_LIBRARY_VERSIONS = {\n'
    for lib_name, version in versions.items():
        version_content += f'    "{lib_name}": "{version}",\n'
    version_content += '}\n'
    
    with open(version_file, 'w') as f:
        f.write(version_content)
    
    print(f"Written JavaScript version info to {version_file}")
    print("Captured versions:")
    for lib_name, version in versions.items():
        print(f"  {lib_name}: {version}")
    
    return versions

def main():
    """Main entry point for command line usage"""
    capture_js_versions()

if __name__ == "__main__":
    main()
