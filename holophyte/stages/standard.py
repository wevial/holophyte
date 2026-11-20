"""The "Holophyte standard" pipeline as a document resolved from the config."""
from holophyte.agents.agent_turns import argv_label, route_labels
from holophyte.agents.harness import critic_seat
from holophyte.config.agent_settings import agent_command, review_route
from holophyte.config.config_tables import merge_config, trim_config, verify_config
from holophyte.config.pipeline_settings import pipeline_notes
from holophyte.config.review_settings import review_config
from holophyte.redact import known_secrets, outbound

NAME = "Holophyte standard"
FORMAT = 1
EFFORT_KEY = "model_reasoning_effort"
REVIEW_ROLES = ["review", "adjudicate", "adversary", "consolidate"]


def resolve(project, profile=None):
    labels = route_labels(project)
    merge = merge_config(project)
    verify = verify_config(project)
    critic = critic_seat(project)
    stages = [
        stage("critic", "check", "ticket", seat=critic_label(project, critic),
              switch=critic is not None),
        stage("implement", "build", "candidate", ["implement"],
              seat=labels["implementer"], profile=profile),
        stage("trim", "build", "candidate", ["trim"], seat=labels["trimmer"],
              switch=trim_config(project).enabled),
        stage("verify", "check", "candidate",
              commands={"always": list(verify.always),
                        "before_merge": list(verify.before_merge)}),
        stage("review", "check", "candidate", REVIEW_ROLES,
              seat=labels["reviewer"], effort=review_effort(project),
              adversary=review_config(project).adversary),
        stage("merge_gate", "check", "factory"),
    ]
    if merge.mode == "pr":
        stages += [
            stage("capture", "check", "factory",
                  switch=bool(merge.ui_capture.strip()),
                  paths=list(merge.ui_paths)),
            stage("covering_fix", "build", "factory", ["implement"],
                  cap=merge.covering_fix_rounds),
            stage("check_fix", "build", "factory", ["implement"],
                  cap=merge.check_fix_rounds),
            stage("capture_fix", "build", "factory", ["implement"],
                  cap=merge.capture_fix_rounds),
        ]
    stages.append(stage("waiting", "check", "factory", approve=merge.approve))
    document = {"format": FORMAT, "name": NAME,
                "merge": {"mode": merge.mode, "approve": merge.approve},
                "stages": stages}
    notes = pipeline_notes(project)
    if notes:
        document["notes"] = notes
    return scrubbed(document, known_secrets(project.config()))


def scrubbed(value, secrets):
    if isinstance(value, str):
        return outbound(value, secrets)
    if isinstance(value, dict):
        return {key: scrubbed(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [scrubbed(item, secrets) for item in value]
    return value


def stage(stage_id, kind, group, roles=(), **settings):
    return {"id": stage_id, "type": kind, "group": group, "roles": list(roles),
            **settings}


def critic_label(project, seat):
    if seat is None:
        return None
    return outbound(argv_label(seat.turn("")[:-1]),
                    known_secrets(project.config()))


def review_effort(project):
    argv = agent_command(project, "review", "")
    if argv is None:
        return review_route(project)[1]
    effort = None
    for flag, value in options(argv[1:]):
        key, assigned, setting = value.partition("=")
        if flag == "--effort":
            effort = value
        elif (flag in ("-c", "--config") and assigned
              and key.strip() == EFFORT_KEY):
            effort = setting.strip().strip("\"'")
    return effort


def options(argv):
    for at, word in enumerate(argv):
        flag, joined, value = word.partition("=")
        if joined and flag.startswith("--"):
            yield flag, value
        elif at + 1 < len(argv):
            yield word, argv[at + 1]
