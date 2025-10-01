const fs = require('fs');
const path = require('path');
const { getMaxGitVersion } = require('./get-version');

try {
  const version = getMaxGitVersion();

  // Read package.json
  const packagePath = path.join(__dirname, '..', 'package.json');
  const packageJson = JSON.parse(fs.readFileSync(packagePath, 'utf8'));

  // Update version
  packageJson.version = version;
  
  // Write back to package.json
  fs.writeFileSync(packagePath, JSON.stringify(packageJson, null, 2) + '\n');
  
  console.log(`Updated casalib version to ${version}`);
} catch (error) {
  console.error('Failed to update version:', error.message);
  process.exit(1);
}
