"""Pure value constraints shared by plugin configuration and manifest defaults."""

from theater.mcp_plugins.contracts import McpConfigField


def string_constraint_error(field: McpConfigField, value: str) -> str | None:
    if field.min_length is not None and len(value) < field.min_length:
        return f"must be at least {field.min_length} characters"
    if field.max_length is not None and len(value) > field.max_length:
        return f"must be at most {field.max_length} characters"
    if field.choices is not None and value not in field.choices:
        return "must be one of the declared choices"
    return None


def numeric_constraint_error(field: McpConfigField, value: int | float) -> str | None:
    if field.minimum is not None and value < field.minimum:
        return f"must be >= {field.minimum}"
    if field.maximum is not None and value > field.maximum:
        return f"must be <= {field.maximum}"
    return None


def item_count_constraint_error(field: McpConfigField, count: int) -> str | None:
    if field.min_items is not None and count < field.min_items:
        return f"must contain at least {field.min_items} items"
    if field.max_items is not None and count > field.max_items:
        return f"must contain at most {field.max_items} items"
    return None
