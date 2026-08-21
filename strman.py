"""strman: small string utilities."""


def is_palindrome(s):
    """Return True if s is a palindrome, case-insensitive and ignoring
    non-alphanumeric characters. Empty strings count as palindromes."""
    cleaned = [c.lower() for c in s if c.isalnum()]
    return cleaned == cleaned[::-1]
