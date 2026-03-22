from videocp.browser import merge_node_options


def test_merge_node_options_deduplicates_flags():
    merged = merge_node_options("--trace-warnings", ["--no-deprecation", "--trace-warnings"])
    assert merged.split() == ["--trace-warnings", "--no-deprecation"]
