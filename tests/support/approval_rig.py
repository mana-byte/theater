"""Launch builders shared by the approval-policy tests."""

from __future__ import annotations


def native_wildcard_match(value, pattern):
    import re

    if not isinstance(pattern, str):
        return False
    escaped = re.escape(pattern.replace("\\", "/")).replace("\\*", ".*")
    return re.fullmatch(escaped, value.replace("\\", "/"), re.DOTALL) is not None


def native_merge(*rulesets):
    """Permission.merge is plain array concatenation (index.ts:200)."""
    return [rule for ruleset in rulesets for rule in ruleset]


def native_evaluate(rules, permission, pattern="*"):
    """evaluate(): the LAST matching rule wins, fallback `ask` (index.ts:28)."""
    for rule in reversed(rules):
        if native_wildcard_match(permission, rule["permission"]) and native_wildcard_match(
            pattern, rule["pattern"]
        ):
            return rule["action"]
    return "ask"
