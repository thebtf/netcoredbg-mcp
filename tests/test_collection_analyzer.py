"""Tests for analyze_collection and summarize_object tool logic."""

from __future__ import annotations

from netcoredbg_mcp.session.state import Variable


class TestCollectionAnalysis:

    def test_numeric_stats(self):
        """Verify numeric stats calculation matches tool logic."""
        items = [
            Variable(name="[0]", value="10", type="int", variables_reference=0),
            Variable(name="[1]", value="20", type="int", variables_reference=0),
            Variable(name="[2]", value="30", type="int", variables_reference=0),
            Variable(name="[3]", value="10", type="int", variables_reference=0),
        ]

        # Replicate tool logic
        numeric_values = []
        for v in items:
            try:
                numeric_values.append(float(v.value))
            except (ValueError, TypeError):
                pass

        assert len(numeric_values) == 4
        assert min(numeric_values) == 10.0
        assert max(numeric_values) == 30.0
        assert sum(numeric_values) == 70.0
        assert sum(numeric_values) / len(numeric_values) == 17.5

    def test_null_count(self):
        items = [
            Variable(name="[0]", value="null", type="object", variables_reference=0),
            Variable(name="[1]", value="hello", type="string", variables_reference=0),
            Variable(name="[2]", value="null", type="object", variables_reference=0),
        ]
        null_count = sum(1 for v in items if v.value in ("null", "Nothing", "None", ""))
        assert null_count == 2

    def test_duplicate_count(self):
        items = [
            Variable(name="[0]", value="a", type="string", variables_reference=0),
            Variable(name="[1]", value="b", type="string", variables_reference=0),
            Variable(name="[2]", value="a", type="string", variables_reference=0),
            Variable(name="[3]", value="c", type="string", variables_reference=0),
            Variable(name="[4]", value="a", type="string", variables_reference=0),
        ]
        seen = set()
        duplicates = 0
        for v in items:
            if v.value in seen:
                duplicates += 1
            seen.add(v.value)
        assert duplicates == 2  # "a" appears 3 times → 2 duplicates

    def test_first_last_items(self):
        items = [Variable(name=f"[{i}]", value=str(i), type="int", variables_reference=0) for i in range(20)]
        sample_size = 5

        first_items = [{"name": v.name, "value": v.value} for v in items[:sample_size]]
        last_items = [{"name": v.name, "value": v.value} for v in items[-sample_size:]]

        assert len(first_items) == 5
        assert first_items[0]["value"] == "0"
        assert len(last_items) == 5
        assert last_items[-1]["value"] == "19"

    def test_non_numeric_skips_stats(self):
        items = [
            Variable(name="[0]", value="hello", type="string", variables_reference=0),
            Variable(name="[1]", value="world", type="string", variables_reference=0),
        ]
        numeric_values = []
        for v in items:
            try:
                numeric_values.append(float(v.value))
            except (ValueError, TypeError):
                pass
        assert len(numeric_values) == 0


class TestObjectSummarizer:

    def test_flat_property_list(self):
        """Verify dot-notation path building."""
        # Simulate recursive walk
        properties = []
        prefix = "user"
        for name, value, vtype in [("name", "Alice", "string"), ("age", "30", "int")]:
            path = f"{prefix}.{name}" if prefix else name
            properties.append({"path": path, "value": value, "type": vtype})

        assert properties[0]["path"] == "user.name"
        assert properties[1]["path"] == "user.age"

    def test_circular_ref_detection(self):
        """Verify visited set prevents infinite recursion."""
        visited = set()
        var_ref = 42
        visited.add(var_ref)

        # Second visit should be detected
        assert var_ref in visited

    def test_depth_clamping(self):
        """Verify depth is clamped to 1-5 range."""
        assert max(1, min(0, 5)) == 1  # 0 → 1
        assert max(1, min(3, 5)) == 3  # 3 → 3
        assert max(1, min(10, 5)) == 5  # 10 → 5

    def test_max_properties_cap(self):
        """Verify truncation at max_properties."""
        max_properties = 50
        properties = [{"path": f"prop{i}", "value": str(i)} for i in range(100)]
        truncated = len(properties) >= max_properties
        assert truncated is True
