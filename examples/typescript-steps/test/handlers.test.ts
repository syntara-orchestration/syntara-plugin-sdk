import assert from 'node:assert/strict';
import { test } from 'node:test';
import { InvalidInputError, normalizeRecords } from '../src/handlers.js';

test('normalizeRecords trims and lowercases every record', () => {
  assert.deepEqual(normalizeRecords({ records: ['  Alpha ', 'BETA', ' École'] }), {
    count: 3,
    normalized: ['alpha', 'beta', 'école'],
  });
});

test('normalizeRecords rejects the wrong shape', () => {
  for (const inputs of [{}, { records: 'alpha' }, { records: [1] }]) {
    assert.throws(() => normalizeRecords(inputs), InvalidInputError);
  }
});

// The manifest's input contract declares additionalProperties: false, so an
// undeclared field must be rejected exactly like a wrong-typed records value.
test('normalizeRecords rejects undeclared fields', () => {
  assert.throws(() => normalizeRecords({ records: [], unexpected: true }), InvalidInputError);
});
