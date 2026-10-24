"""A turn's proposals: read from the lead's JSON and posted as thread cards."""
from store.scout_thread import ANSWER, ASK, STEER, post_message

MAX_PROPOSALS = 2
CHOICE_MODES = (ASK, STEER)
MIN_CHOICES, MAX_CHOICES = 2, 4
PROPOSAL = "proposal"
PROPOSALS_SHAPE = ('"proposals": [{"question": "...", "choices": [{"label": '
                   '"...", "mode": "ask"}, {"label": "...", "mode": "steer"}]}]')


def text(value):
    return isinstance(value, str) and bool(value.strip())


def choice_problem(number, choice):
    if not isinstance(choice, dict) or not text(choice.get("label")):
        return f"choice {number}: label must be non-blank text"
    if choice.get("mode") not in CHOICE_MODES:
        return f"choice {number}: mode must be one of {', '.join(CHOICE_MODES)}"
    return None


def proposal_problem(proposal):
    if not isinstance(proposal, dict) or not text(proposal.get("question")):
        return "question must be non-blank text"
    choices = proposal.get("choices")
    if (not isinstance(choices, list)
            or not MIN_CHOICES <= len(choices) <= MAX_CHOICES):
        return f"it must offer {MIN_CHOICES} to {MAX_CHOICES} choices"
    return next(filter(None, (choice_problem(number, choice) for number, choice
                              in enumerate(choices, 1))), None)


def read_proposals(document):
    given = document.get("proposals") if isinstance(document, dict) else None
    if not isinstance(given, list):
        return [], [] if given is None else ["proposals: must be a list"]
    kept, dropped = [], []
    for number, proposal in enumerate(given, 1):
        problem = proposal_problem(proposal)
        if problem is None and len(kept) >= MAX_PROPOSALS:
            problem = f"past the cap of {MAX_PROPOSALS}"
        if problem is None:
            kept.append({"question": proposal["question"].strip(),
                         "choices": [{"label": choice["label"].strip(),
                                      "mode": choice["mode"]}
                                     for choice in proposal["choices"]]})
        else:
            dropped.append(f"proposal {number}: {problem}")
    return kept, dropped


def post_cards(conn, scout_id, answers_id, proposals):
    return [post_message(conn, scout_id, "scout", ANSWER, proposal["question"],
                         blocks=[{"type": PROPOSAL, **proposal}],
                         answers_id=answers_id)
            for proposal in proposals]


def proposals_goal():
    return ("\nYou may add a \"proposals\" list to the fenced JSON object "
            f"you end with: at most {MAX_PROPOSALS} questions you would put "
            f"to the person before guessing, each with {MIN_CHOICES} to "
            f"{MAX_CHOICES} choices, a choice's mode \"ask\" for an answer "
            "or \"steer\" for another pass, in this shape: "
            f"{{{PROPOSALS_SHAPE}}}\n")


def answered_card(thread, message):
    if message.answers_id is None:
        return None
    return next((earlier.text for earlier in thread
                 if earlier.id == message.answers_id), None)


def card_line(thread, message):
    question = answered_card(thread, message)
    return ("" if question is None
            else f"It answers your earlier question: {question}\n\n")
