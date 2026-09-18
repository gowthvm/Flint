from core import history


def test_audited_history_entries_form_a_hash_chain(tmp_path):
    history.HISTORY_PATH = tmp_path / "history.json"

    first = history.append_audited_history({"job_id": "one", "state": "passed"})
    second = history.append_audited_history({"job_id": "two", "state": "failed"})

    assert first["integrity_sha256"] == history.history_entry_digest(first)
    assert second["integrity_prev"] == first["integrity_sha256"]
    assert history.verify_history_integrity() == (True, None)


def test_history_integrity_detects_tampering(tmp_path):
    history.HISTORY_PATH = tmp_path / "history.json"
    history.append_audited_history({"job_id": "one", "state": "passed"})
    entries = history.load_history()
    entries[0]["state"] = "failed"
    history.save_history(entries)

    assert history.verify_history_integrity() == (False, 0)


def test_legacy_entries_do_not_fail_integrity_check(tmp_path):
    history.HISTORY_PATH = tmp_path / "history.json"
    history.save_history([{"job_id": "legacy", "state": "passed"}])

    assert history.verify_history_integrity() == (True, None)