# Deploy — units the factory ships but does not start

Process management is the operator's; the factory ships the invocation and
nothing around it. What lives here is the checked-in shape of that management
on a writer host, to copy under `~/.config/systemd/user/` and enable by hand.

## Files

- `deploy/holophyte-serve@.service` — systemd **user** unit template, one
  instance per target, running `factory.py TARGET --serve ADDRESS:PORT`
  (the long form of `--serve 7710`, so the address is a key of its own) with
  `Restart=on-failure`. The instance name is the target slug
  (`holophyte-serve@holophyte`); the target path, bind address and port come
  from `~/.holophyte/SLUG/serve.env`. Setup, the port convention and the
  environment file's keys are in `docs/operating.md` under "Serving standing".

macOS writers have no systemd; a launchd equivalent is not shipped.
