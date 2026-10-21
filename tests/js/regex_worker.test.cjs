const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const workerSource = fs.readFileSync(
  path.join(__dirname, '../../static/js/tools/regex_worker.js'),
  'utf8'
);

function executeWorker(message) {
  let onMessage;
  let result;
  const self = {
    addEventListener(type, callback) {
      if (type === 'message') onMessage = callback;
    },
    postMessage(value) {
      result = value;
    },
  };
  vm.runInNewContext(workerSource, { self, RegExp, Array, String, Error });
  onMessage({ data: message });
  return result;
}

test('worker returns global matches and capture groups', () => {
  const result = executeWorker({ pattern: '(\\w+)-(\\d+)', flags: 'g', text: 'item-42 item-17' });
  assert.equal(result.ok, true);
  assert.equal(result.matches.length, 2);
  assert.equal(result.matches[0].text, 'item-42');
  assert.deepEqual(Array.from(result.matches[0].captures), ['item', '42']);
});

test('worker advances after empty unicode matches', () => {
  const result = executeWorker({ pattern: '(?=.)', flags: 'gu', text: 'a😀b', maxMatches: 10 });
  assert.equal(result.ok, true);
  assert.deepEqual(Array.from(result.matches, (match) => match.index), [0, 1, 3]);
});

test('worker caps match output only when there are additional matches', () => {
  const result = executeWorker({ pattern: '.', flags: 'g', text: 'abcdef', maxMatches: 3 });
  assert.equal(result.ok, true);
  assert.equal(result.matches.length, 3);
  assert.equal(result.capped, true);
  const exact = executeWorker({ pattern: '.', flags: 'g', text: 'abc', maxMatches: 3 });
  assert.equal(exact.matches.length, 3);
  assert.equal(exact.capped, false);
});

test('worker reports malformed patterns without throwing', () => {
  const result = executeWorker({ pattern: '[', flags: 'g', text: 'abc' });
  assert.equal(result.ok, false);
  assert.match(result.error, /unterminated|invalid/i);
});
