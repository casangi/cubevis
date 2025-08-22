const esbuild = require('esbuild');
const packageJson = require('../package.json');
const fs = require('fs');
const path = require('path');

// Parse command line arguments
const isProduction = process.argv.includes('--production');
const isDev = process.argv.includes('--dev');
const isWatch = process.argv.includes('--watch');

// Get version from package.json
const version = packageJson.version;

// Determine output filename based on build type
const getOutputFile = () => {
  if (isDev) {
    return `dist/esbuild/casalib-${version}.js`;
  }
  return `dist/esbuild/casalib-${version}.min.js`;
};

// Ensure output directory exists
const outputDir = 'dist/esbuild';
if (!fs.existsSync(outputDir)) {
  fs.mkdirSync(outputDir, { recursive: true });
}

// Base esbuild configuration
const config = {
  entryPoints: ['src/browser.ts'],
  bundle: true,
  sourcemap: 'external',
  platform: 'browser',
  define: {
    __VERSION__: `"${version}"`
  },
  outfile: getOutputFile()
};

// Add minification for production builds
if (isProduction || (!isDev && !isWatch)) {
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
      }
    }
  };
  console.log(`Watching for changes... Output: ${config.outfile}`);
}

// Build the bundle
esbuild.build(config)
  .then((result) => {
    if (!isWatch) {
      console.log(`✅ Built ${config.outfile}`);
      
      // Create unversioned copies in both locations
      const rootDistDir = 'dist';
      if (!fs.existsSync(rootDistDir)) {
        fs.mkdirSync(rootDistDir, { recursive: true });
      }
      
      // Copy to root dist/
      const rootUnversionedFile = isDev ? 'dist/casalib.js' : 'dist/casalib.min.js';
      fs.copyFileSync(config.outfile, rootUnversionedFile);
      console.log(`✅ Copied to ${rootUnversionedFile}`);
      
      // Copy to dist/esbuild/ as well 📋
      const esbuildUnversionedFile = isDev ? 'dist/esbuild/casalib.js' : 'dist/esbuild/casalib.min.js';
      fs.copyFileSync(config.outfile, esbuildUnversionedFile);
      console.log(`✅ Copied to ${esbuildUnversionedFile}`);
    }
  })
  .catch((error) => {
    console.error('Build failed:', error);
    process.exit(1);
  });
