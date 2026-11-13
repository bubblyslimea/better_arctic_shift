import assert from 'node:assert/strict';
import fs from 'node:fs';
import {execFileSync} from 'node:child_process';
import {captureStates, observationRank} from '../better_arctic_shift/history.mjs';

const cases = JSON.parse(fs.readFileSync(new URL('fixtures/history-contract.json', import.meta.url), 'utf8'));
const python = JSON.parse(execFileSync('python3', ['-c', "import json\nfrom better_arctic_shift.history import capture_states\ncases=json.load(open('tests/fixtures/history-contract.json'))\nprint(json.dumps([capture_states(c['record'],c['kind'],c['source'],c['captured']) for c in cases]))"], {encoding: 'utf8'}));
for (const [index, c] of cases.entries()) {
  const actual = captureStates(c.record, c.kind, c.source, c.captured);
  assert.deepEqual(actual, c.expected, c.name);
  assert.deepEqual(actual, python[index], 'browser and Python: ' + c.name);
}
const older = {source: 'arctic_shift', source_time: '1970-01-01T00:05:00Z', content_time: '1970-01-01T00:01:40Z'};
assert.equal(observationRank(older, 'present')[0], 300);
assert.equal(observationRank(older, 'present', true)[0], 100);
assert.equal(observationRank({...older, content_time: null}, 'present', true)[0], -1);
console.log(`${cases.length} history cases match across Python and the browser`);
