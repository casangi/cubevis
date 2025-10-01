const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

// Read package.json to get cubevis version
const packageJson = JSON.parse(fs.readFileSync('package.json', 'utf8'));
const cubevisVersion = packageJson.version;

// Get the actual bokeh version from the command line
let bokehVersion;
try {
  const bokehVersionOutput = execSync('bokeh --version', { encoding: 'utf8' }).trim();
  bokehVersion = bokehVersionOutput;
  console.log(`Detected Bokeh version: ${bokehVersion}`);
} catch (error) {
  console.error('Could not detect bokeh version from command line');
  console.error('Falling back to @bokeh/bokehjs version from package.json');
  bokehVersion = packageJson.dependencies['@bokeh/bokehjs'];
}

// Extract major.minor from bokeh version (remove patch and any pre-release info)
const bokehMajorMinor = bokehVersion.split('.').slice(0, 2).join('.');

// Define files to copy with versioning
const filesToCopy = [
  { 
    source: 'dist/cubevisjs.min.js', 
    dest: `../cubevis/__js__/bokeh-${bokehMajorMinor}/cubevisjs.min.js`,
    description: 'Minified library'
  }
];

let copiedCount = 0;
let errorCount = 0;

console.log(`Creating versioned copies (v${cubevisVersion}, Bokeh ${bokehMajorMinor}):`);

// Copy each file
for (const file of filesToCopy) {
  if (!fs.existsSync(file.source)) {
    console.warn(`\n⚠️ Skipping ${file.description}: ${file.source} not found`);
    continue;
  }

  try {
    // Get the destination directory path
    const destDir = path.dirname(file.dest);

    // Create the directory and any parent directories, if they don't exist
    // The `recursive: true` option is essential for creating nested directories
    fs.mkdirSync(destDir, { recursive: true });    
      
    fs.copyFileSync(file.source, file.dest);
    console.log(`\n✅ ${file.description}: ${path.basename(file.dest)}`);
    copiedCount++;
  } catch (error) {
    console.error(`\n❌ Failed to copy ${file.description}: ${error.message}`);
    errorCount++;
  }
}

console.log(`\nSummary: ${copiedCount} files copied, ${errorCount} errors`);
if (errorCount > 0) {
  process.exit(1);
}
