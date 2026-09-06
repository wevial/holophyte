// Target of the package.json `install` script.
//
// Bun 1.3 and 1.4 parse a space-separated `--cwd` (`bun --cwd console install
// --frozen-lockfile`) as `bun run install --frozen-lockfile` inside console/,
// not as `bun install`; and `bun --cwd console run build` under the same
// parsing prints `bun run` usage and exits 0 without building. This file
// gives those two documented commands their intended effect: it runs the real
// `bun install` with the flags it was handed, then the build, so the chain
// `install` -> `test` -> `run build` leaves `dist/` populated.
//
// A bare `bun install` inside console/ also runs this file, as the package's
// `install` lifecycle hook; that invocation lacks `npm_command=run-script`
// and is skipped. The nested `bun install` below marks its environment so
// its own hook is skipped too.

const invokedByBunRun = process.env.npm_command === "run-script";
const alreadyInstalling = process.env.HOLOPHYTE_CONSOLE_INSTALLING === "1";

if (invokedByBunRun && !alreadyInstalling) {
  const run = (cmd: string[], extraEnv: Record<string, string> = {}) => {
    const { exitCode } = Bun.spawnSync(cmd, {
      cwd: import.meta.dir,
      stdio: ["inherit", "inherit", "inherit"],
      env: { ...process.env, ...extraEnv },
    });
    if (exitCode !== 0) process.exit(exitCode);
  };
  run(["bun", "install", ...process.argv.slice(2)], { HOLOPHYTE_CONSOLE_INSTALLING: "1" });
  run(["bun", "run", "build"]);
}
