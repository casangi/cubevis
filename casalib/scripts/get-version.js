const { execSync } = require('child_process');

function compareVersions(a, b) {
  // Remove 'v' prefix and split into parts
  const aParts = a.replace(/^v/, '').split('.').map(Number);
  const bParts = b.replace(/^v/, '').split('.').map(Number);

  for (let i = 0; i < Math.max(aParts.length, bParts.length); i++) {
    const aVal = aParts[i] || 0;
    const bVal = bParts[i] || 0;

    if (aVal !== bVal) {
      return aVal - bVal;
    }
  }
  return 0;
}

function getMaxGitVersion() {
  try {
    // Get all tags matching version pattern
    const tags = execSync('git tag')
      .toString()
      .trim()
      .split('\n')
      .filter(tag => /^v\d+\.\d+\.\d+$/.test(tag));

    if (tags.length === 0) {
      throw new Error('No version tags found');
    }

    // Sort and get the maximum version
    tags.sort(compareVersions);
    const maxTag = tags[tags.length - 1];
    return maxTag.replace(/^v/, '');
  } catch (error) {
    console.error('Failed to get git version:', error.message);
    throw error;
  }
}

module.exports = { getMaxGitVersion };
