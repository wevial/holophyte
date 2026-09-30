def select_arm(mode, run_id):
    if mode == 'alternate':
        return 'resume' if run_id is not None and run_id % 2 else 'fresh'
    return mode
