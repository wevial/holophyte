MILLION = 1_000_000


def turn_cost(price, tokens, reported):
    if reported is not None:
        return reported
    if price is None or tokens is None:
        return None
    return round(tokens * price / MILLION, 6)
