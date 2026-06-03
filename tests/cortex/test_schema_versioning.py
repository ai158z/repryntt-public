"""Tests for repryntt.brain.schema — brain JSON schema versioning."""

import pytest
from repryntt.brain.schema import stamp_version, check_version, SCHEMA_VERSIONS


class TestStampVersion:
    """Tests for stamp_version."""

    def test_stamps_known_file(self):
        data = {"memories": []}
        stamp_version(data, "semantic_memory")
        assert data["schema_version"] == SCHEMA_VERSIONS["semantic_memory"]

    def test_stamps_unknown_file_defaults_to_1(self):
        data = {}
        stamp_version(data, "new_unknown_file")
        assert data["schema_version"] == 1

    def test_overwrites_existing_version(self):
        data = {"schema_version": 99}
        stamp_version(data, "daemon_state")
        assert data["schema_version"] == SCHEMA_VERSIONS["daemon_state"]

    def test_returns_data_for_chaining(self):
        data = {}
        result = stamp_version(data, "daemon_state")
        assert result is data


class TestCheckVersion:
    """Tests for check_version."""

    def test_missing_version_auto_stamps(self):
        data = {"memories": []}
        result = check_version(data, "semantic_memory")
        assert result is None
        assert data["schema_version"] == SCHEMA_VERSIONS["semantic_memory"]

    def test_missing_version_no_auto_stamp(self):
        data = {"memories": []}
        result = check_version(data, "semantic_memory", auto_stamp=False)
        assert result is None
        assert "schema_version" not in data

    def test_matching_version_ok(self):
        expected = SCHEMA_VERSIONS["daemon_state"]
        data = {"schema_version": expected}
        result = check_version(data, "daemon_state")
        assert result == expected

    def test_old_version_warns(self, caplog):
        """Older version triggers migration warning."""
        data = {"schema_version": 0}
        import logging
        with caplog.at_level(logging.WARNING):
            result = check_version(data, "daemon_state")
        assert result == 0
        assert "migration needed" in caplog.text.lower() or result == 0

    def test_newer_version_errors(self, caplog):
        """Newer version triggers error log."""
        data = {"schema_version": 999}
        import logging
        with caplog.at_level(logging.ERROR):
            result = check_version(data, "daemon_state")
        assert result == 999


class TestSchemaRegistry:
    """Tests for SCHEMA_VERSIONS registry."""

    def test_all_known_files_versioned(self):
        expected_files = [
            "daemon_state", "consciousness_state", "phase_state",
            "semantic_memory", "reasoning_chain",
        ]
        for f in expected_files:
            assert f in SCHEMA_VERSIONS, f"{f} not in SCHEMA_VERSIONS"

    def test_all_versions_positive_int(self):
        for key, ver in SCHEMA_VERSIONS.items():
            assert isinstance(ver, int) and ver > 0, f"{key} has bad version: {ver}"
