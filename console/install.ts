// Target of the package.json `install` script.
//
// Bun 1.3.14 and 1.4.2 parse a space-separated `--cwd` (`bun --cwd console
// install --frozen-lockfile`) as `bun run install --frozen-lockfile` inside
// console/, not as `bun install`; and `bun --cwd console run build` under the
// same parsing prints `bun run` usage and exits 0 without building — no
// script name reaches the package. This file gives the first command its
// intended effect: it runs the real `bun install` with the flags it was
// handed, then the build, so the chain `install` -> `test` -> `run build`
// leaves `dist/` populated. The build itself is witnessed by
// tests/build.test.ts under the `test` line, not by this hook, and the
// notice below says the build is happening so it is never mistaken for the
// `run build` line's doing.
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
  console.log("console/install.ts: building console/dist/ (`bun --cwd console run build` is a no-op on Bun 1.3/1.4; use `bun --cwd=console run build` to rebuild)");
  run(["bun", "run", "build"]);
}
