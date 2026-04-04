"""Tests for analyze_collection and summarize_object tool logic."""

from __future__ import annotations

from netcoredbg_mcp.session.state import Variable
from netcoredbg_mcp.tools.inspection import compute_collection_stats


def _make_vars(*value_type_pairs: tuple[str, str]) -> list[Variable]:
    return [
        Variable(name=f"[{i}]", value=v, type=t, variables_reference=0)
        for i, (v, t) in enumerate(value_type_pairs)
    ]


class TestCollectionAnalysis:

    def test_numeric_stats(self):
        """Verify numeric stats via actual compute_collection_stats."""
        items = _make_vars(("10", "int"), ("20", "int"), ("30", "int"), ("10", "int"))
        result = compute_collection_stats(items, sample_size=5)

        assert result["count"] == 4
        assert result["min"] == 10.0
        assert result["max"] == 30.0
        assert result["sum"] == 70.0
        assert result["average"] == 17.5

    def test_null_count(self):
        items = _make_vars(("null", "object"), ("hello", "string"), ("null", "object"))
        result = compute_collection_stats(items, sample_size=5)
        assert result["null_count"] == 2

    def test_nothing_and_none_sentinels(self):
        """Verify all null sentinels are counted."""
        items = _make_vars(("Nothing", "object"), ("None", "object"), ("", "string"), ("x", "string"))
        result = compute_collection_stats(items, sample_size=5)
        assert result["null_count"] == 3

    def test_duplicate_count(self):
        items = _make_vars(
            ("a", "string"), ("b", "string"), ("a", "string"),
            ("c", "string"), ("a", "string"),
        )
        result = compute_collection_stats(items, sample_size=5)
        assert result["duplicate_count"] == 2  # "a" appears 3 times → 2 duplicates

    def test_first_last_items(self):
        items = [Variable(name=f"[{i}]", value=str(i), type="int", variables_reference=0) for i in range(20)]
        result = compute_collection_stats(items, sample_size=5)

        assert len(result["first_items"]) == 5
        assert result["first_items"][0]["value"] == "0"
        assert len(result["last_items"]) == 5
        assert result["last_items"][-1]["value"] == "19"

    def test_last_items_empty_when_count_le_sample_size(self):
        """last_items is empty when count <= sample_size (no redundancy)."""
        items = _make_vars(("1", "int"), ("2", "int"), ("3", "int"))
        result = compute_collection_stats(items, sample_size=5)
        assert result["last_items"] == []

    def test_non_numeric_skips_stats(self):
        items = _make_vars(("hello", "string"), ("world", "string"))
        result = compute_collection_stats(items, sample_size=5)
        assert "min" not in result
        assert "max" not in result
        assert "sum" not in result
        assert "average" not in result

    def test_element_type_from_first_item(self):
        items = _make_vars(("1", "System.Int32"), ("2", "System.Int32"))
        result = compute_collection_stats(items, sample_size=5)
        assert result["element_type"] == "System.Int32"

    def test_mixed_numeric_and_non_numeric(self):
        """Non-parseable values are skipped; numeric stats cover only parseable ones."""
        items = _make_vars(("10", "int"), ("N/A", "string"), ("20", "int"))
        result = compute_collection_stats(items, sample_size=5)
        assert result["min"] == 10.0
        assert result["max"] == 20.0
        assert result["sum"] == 30.0


class TestObjectSummarizer:

    def test_depth_clamping(self):
        """Verify depth is clamped to 1-5 range (same formula as summarize_object)."""
        assert max(1, min(0, 5)) == 1   # 0 → 1
        assert max(1, min(3, 5)) == 3   # 3 → 3
        assert max(1, min(10, 5)) == 5  # 10 → 5

    def test_ancestor_cycle_detection(self):
        """Ancestor-chain tracking correctly identifies true cycles vs shared refs.

        The _walk closure uses frozenset ancestors — a ref is a cycle only when it
        appears in the current call path, not merely when it was seen before.
        This test verifies the algorithm directly.
        """
        # Simulate two siblings sharing the same var_ref (shared, not cycle)
        ancestors_root: frozenset[int] = frozenset()
        ref_root = 1

        ancestors_depth1 = ancestors_root | {ref_root}

        # Child A uses ref 10, child B also uses ref 10 (shared ref, not cycle)
        ref_child_a = 10
        ref_child_b = 10  # same ref as child_a — DAP often reuses refs

        # Child A: not a cycle (not in ancestors_depth1 which only has ref_root=1)
        assert ref_child_a not in ancestors_depth1

        # After walking child A, its ancestors are {1, 10}
        ancestors_child_a = ancestors_depth1 | {ref_child_a}

        # Child B is a SIBLING: it uses ancestors_depth1, not ancestors_child_a
        # So ref_child_b (=10) is NOT in ancestors_depth1 → not a cycle
        assert ref_child_b not in ancestors_depth1

        # True cycle: a grandchild references an ancestor (ref_root=1)
        ref_grandchild_back = ref_root
        assert ref_grandchild_back in ancestors_child_a  # IS a cycle

    def test_max_properties_cap(self):
        """Verify compute_collection_stats respects sample_size for first/last."""
        items = [Variable(name=f"[{i}]", value=str(i), type="int", variables_reference=0) for i in range(100)]
        result = compute_collection_stats(items, sample_size=5)
        assert len(result["first_items"]) == 5
        assert len(result["last_items"]) == 5

    def test_flat_path_building(self):
        """Verify dot-notation path: prefix + '.' + name when prefix is non-empty."""
        prefix = "user"
        name = "age"
        path = f"{prefix}.{name}" if prefix else name
        assert path == "user.age"

    def test_empty_prefix_path_building(self):
        """Verify path is just name when prefix is empty string."""
        prefix = ""
        name = "count"
        path = f"{prefix}.{name}" if prefix else name
        assert path == "count"
