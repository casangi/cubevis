const fs = require('fs');
const { execSync } = require('child_process');

// Get the current bokeh version
let bokehVersion;
try {
  bokehVersion = execSync('bokeh --version', { encoding: 'utf8' }).trim();
  console.log(`Detected Bokeh version: ${bokehVersion}`);
} catch (error) {
  console.error('Could not detect bokeh version. Is bokeh installed?');
  process.exit(1);
}

// Read current package.json
const packageJson = JSON.parse(fs.readFileSync('package.json', 'utf8'));

// Check if bokehjs version matches
const currentBokehJs = packageJson.dependencies['@bokeh/bokehjs'];
if (currentBokehJs === bokehVersion) {
  console.log(`✓ @bokeh/bokehjs is already at version ${bokehVersion}`);
} else {
  console.log(`Updating @bokeh/bokehjs from ${currentBokehJs} to ${bokehVersion}`);
  
  // Update the version
  packageJson.dependencies['@bokeh/bokehjs'] = bokehVersion;
  
  // Write back to package.json
  fs.writeFileSync('package.json', JSON.stringify(packageJson, null, 2) + '\n');
  
  console.log('✓ package.json updated');
  console.log('Run "npm install" to install the updated version');
}
