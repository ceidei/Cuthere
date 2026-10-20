const assert = require('node:assert/strict');
const test = require('node:test');
const { releasePreviewSnapshot } = require('../premiere_cep/review_capture_panel/js/review_panel.js');

test('moving playhead keeps the F8 release position with a review flag', () => {
  const start = {
    project: { path: 'F:\\SampleProject.prproj' },
    sequence: { host_id: 'sequence-1' },
    location: { ticks: '100' },
    content_fingerprint: 'fnv32x2:abc',
    content_fingerprint_status: 'partial',
  };
  const preview = { position: { ticks: '200', time_label: '00:00:00:02' } };
  const end = releasePreviewSnapshot(start, preview);
  assert.equal(end.location.ticks, '200');
  assert.equal(end.project.path, start.project.path);
  assert.equal(end.sequence.host_id, start.sequence.host_id);
  assert.equal(end.content_fingerprint, start.content_fingerprint);
  assert.equal(end.capture_quality, 'release_preview_only');
});
