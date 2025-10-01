const esbuild = require('esbuild');
const fs = require('fs');
const path = require('path');
const { getMaxGitVersion } = require('./get-version');

// Parse command line arguments
const isProduction = process.argv.includes('--production');
const isDev = process.argv.includes('--dev');
const isWatch = process.argv.includes('--watch');

// Get version from git tags (fallback to package.json if git fails)
let version;
try {
  version = getMaxGitVersion();
  console.log(`Using git version: ${version}`);
} catch (error) {
  console.warn('Could not get version from git, falling back to package.json');
  const packageJson = require('../package.json');
  version = packageJson.version;
}

// Determine if we should minify (production or when neither dev nor watch is specified)
const shouldMinify = isProduction || (!isDev && !isWatch);

// Define files to copy with versioning
const filesToCopy = [
  {
    source: 'dist/esbuild/casalib.min.js',
    dest: '../cubevis/__js__/casalib.min.js',
    description: 'Published library'
  }
];

// Always create the versioned filename as the primary output
const getVersionedOutputFile = () => {
  if (shouldMinify) {
    return `dist/esbuild/casalib-${version}.min.js`;
  }
  return `dist/esbuild/casalib-${version}.js`;
};

// Ensure output directory exists
const outputDir = 'dist/esbuild';
if (!fs.existsSync(outputDir)) {
  fs.mkdirSync(outputDir, { recursive: true });
}

let copiedCount = 0;
let errorCount = 0;

// Base esbuild configuration
const config = {
  entryPoints: ['src/browser.ts'],
  bundle: true,
  sourcemap: 'external',
  platform: 'browser',
  outfile: getVersionedOutputFile()
};

// Add minification for production builds
if (shouldMinify) {
  config.minify = true;
}

// Add watch mode if requested
if (isWatch) {
  config.watch = {
    onRebuild(error, result) {
      if (error) {
        console.error('Watch build failed:', error);
      } else {
        console.log(`Rebuilt ${config.outfile}`);
        // Also copy the unversioned files on rebuild
        copyFilesToDist(filesToCopy);
      }
    }
  };
  console.log(`Watching for changes... Output: ${config.outfile}`);
}

// Function to copy unversioned files
function copyFilesToDist(copySpecs) {
  // Copy each file
  for (const file of copySpecs) {
    if (!fs.existsSync(file.source)) {
      console.warn(`⚠️  Skipping ${file.description}: ${file.source} not found`);
      continue;
    }

    try {
      fs.copyFileSync(file.source, file.dest);
      console.log(`\n✅ ${file.description}: ${path.basename(file.dest)}`);
      copiedCount++;
    } catch (error) {
      console.error(`\n❌ Failed to copy ${file.description}: ${error.message}`);
      errorCount++;
    }
  }
}

// Build the bundle
esbuild.build(config)
  .then((result) => {
    console.log(`\n✅ Built ${config.outfile} (version ${version})`);

    if (!isWatch) {
      // Create unversioned copies
      copyFilesToDist(filesToCopy);
    }
    console.log(`\nSummary: ${copiedCount} files copied, ${errorCount} errors`);
    if (errorCount > 0) {
      process.exit(1);
    }
  })
  .catch((error) => {
    console.error('Build failed:', error);
    process.exit(1);
  });
