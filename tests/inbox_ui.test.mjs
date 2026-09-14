import assert from 'node:assert/strict';
import test from 'node:test';
import {isWritable, mergeMessagePages, uploadWithinLimit} from '../app/inbox/static/ui.mjs';

test('library pages merge in chronological order without duplicates', () => {
  assert.deepEqual(mergeMessagePages([{id:2}, {id:1}], [{id:2}, {id:3}]).map(x => x.id),
    [1, 2, 3]);
});

test('only active conversations and Saved Messages are writable', () => {
  assert.equal(isWritable('inbox'), true);
  assert.equal(isWritable('library', {writable:true}), true);
  assert.equal(isWritable('library', {writable:false}), false);
});

test('upload limit rejects empty and oversized files', () => {
  assert.equal(uploadWithinLimit({size:1}, 100), true);
  assert.equal(uploadWithinLimit({size:0}, 100), false);
  assert.equal(uploadWithinLimit({size:101}, 100), false);
});
