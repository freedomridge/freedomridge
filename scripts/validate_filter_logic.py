from backend.routers.datasets import parse_filter_node


def test_parse_filter_node():
    args = []
    arg_idx = 1

    # Test 1: Equality on metadata
    node = {"field": "weather", "operator": "=", "value": "rainy"}
    sql, idx = parse_filter_node(node, args, arg_idx)
    print(f"Test 1 (Equality): {sql}")
    print(f"Args: {args}")
    assert "metadata @> $1::jsonb" in sql
    assert args[0] == '{"weather": "rainy"}'

    # Test 2: Equality on numeric metadata
    args = []
    arg_idx = 1
    node = {"field": "score", "operator": "=", "value": 5}
    sql, idx = parse_filter_node(node, args, arg_idx)
    print(f"Test 2 (Numeric Equality): {sql}")
    print(f"Args: {args}")
    assert "metadata @> $1::jsonb" in sql
    assert args[0] == '{"score": 5}'

    # Test 3: IN on metadata
    args = []
    arg_idx = 1
    node = {"field": "weather", "operator": "in", "value": ["rainy", "sunny"]}
    sql, idx = parse_filter_node(node, args, arg_idx)
    print(f"Test 3 (IN): {sql}")
    print(f"Args: {args}")
    assert "OR" in sql
    assert "metadata @> $1::jsonb" in sql
    assert "metadata @> $2::jsonb" in sql
    assert args[0] == '{"weather": "rainy"}'
    assert args[1] == '{"weather": "sunny"}'

    # Test 4: Special field (filepath) - should use normal =
    args = []
    arg_idx = 1
    node = {"field": "filepath", "operator": "=", "value": "/tmp/img.jpg"}
    sql, idx = parse_filter_node(node, args, arg_idx)
    print(f"Test 4 (Special Field): {sql}")
    assert "filepath = $1" in sql
    assert args[0] == "/tmp/img.jpg"

    print("All tests passed!")


if __name__ == "__main__":
    test_parse_filter_node()
