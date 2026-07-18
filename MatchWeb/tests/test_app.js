const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'app.js'),
  'utf8',
);
const start = source.indexOf('function createFilterMarker');
const end = source.indexOf('\nfunction renderMatches', start);
assert.notEqual(start, -1, 'createFilterMarker must exist');
assert.notEqual(end, -1, 'createFilterMarker boundary must exist');

eval(source.slice(start, end));

assert.equal(createFilterMarker(undefined), '—');
assert.equal(createFilterMarker(null), '—');
assert.equal(createFilterMarker([]), '—');
console.log('app.js compatibility tests passed');
