"""Stable experiment assignment shared by implementer and reviewer sessions."""


def select_arm(mode, run_id):
    """Alternate by store run number, keeping every round in one arm."""
    if mode == 'alternate':
        return 'resume' if run_id is not None and run_id % 2 else 'fresh'
    return mode
