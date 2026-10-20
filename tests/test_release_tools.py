from pathlib import Path
import pytest
from scripts.agent_handoff import prepare

def test_invalid_batch_creates_failure_report_but_no_agent_prompt(tmp_path):
    result = prepare(tmp_path)
    assert result['status'] == 'FAIL'
    output = Path(result['report_dir'])
    assert (output / 'review_batch.json').is_file()
    assert not (output / 'AGENT_HANDOFF.md').exists()

def test_handoff_rejects_outside_output_without_writing(tmp_path):
    outside = tmp_path / 'other'
    with pytest.raises(ValueError):
        prepare(tmp_path, outside)
    assert not outside.exists()

def test_handoff_preserves_existing_output(tmp_path):
    output = tmp_path / 'work' / 'premiere-review' / 'existing'
    output.mkdir(parents=True)
    marker = output / 'keep.txt'
    marker.write_text('keep', encoding='utf-8')
    with pytest.raises(FileExistsError):
        prepare(tmp_path, output)
    assert marker.read_text(encoding='utf-8') == 'keep'
