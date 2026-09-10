# Deploy — units the factory ships but does not start

Process management is the operator's; the factory ships the invocation and
nothing around it. What lives here is the checked-in shape of that management
on a writer host, to copy under `~/.config/systemd/user/` and enable by hand.

## Install

The daemon has one Python dependency, `tomlkit`, which `PUT /config` uses
to edit the target's config in place without losing comments; it is pinned
in `requirements.txt`. On the writer host, before enabling the serve unit:

```
python3 -m pip install --user -r requirements.txt
```

A daemon started without it exits with one line naming the module and this
command.

## Files

- `deploy/holophyte-serve@.service` — systemd **user** unit template, one
  instance per target, running `factory.py TARGET --serve ADDRESS:PORT`
  (the long form of `--serve 7710`, so the address is a key of its own) with
  `Restart=on-failure`. The instance name is the target slug
  (`holophyte-serve@holophyte`); the target path, bind address and port come
  from `~/.holophyte/SLUG/serve.env`. Setup, the port convention and the
  environment file's keys are in `docs/operating.md` under "Serving standing".
- `deploy/holophyte-supervise@.service` — the supervisor, `factory.py TARGET
  --supervise`, with `Restart=on-failure`; the instance name and environment
  file are the serve unit's, and only `HOLOPHYTE_TARGET` is read from it.
  Enable with `systemctl --user enable --now holophyte-supervise@NAME`.
- `deploy/holophyte-loop@.service` — one pass of the loop, `factory.py
  TARGET`, `Type=exec` with `Restart=no`. Start it by hand with
  `systemctl --user start holophyte-loop@NAME`: it runs the queue down and
  the unit is inactive again once the loop prints its idle line; a failed
  pass stays visible as a failed unit rather than restarting. Not meant to
  be enabled.

All three log to the journal under their unit name
(`journalctl --user -u holophyte-supervise@NAME`), and none depends on a
shell session: a dead tmux server takes down nothing that runs here.

macOS writers have no systemd; a launchd equivalent is not shipped.
