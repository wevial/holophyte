import tailwind from "bun-plugin-tailwind";

const here = new URL("./", import.meta.url).pathname;

// Bundles index.html (and the script and stylesheet it references) into
// `outdir`. Returns the emitted paths relative to `outdir` so callers can
// report or inspect them; throws with the bundler's logs on failure.
export async function buildConsole(outdir: string = `${here}dist/`): Promise<string[]> {
  const result = await Bun.build({
    entrypoints: [`${here}index.html`],
    outdir,
    plugins: [tailwind],
    minify: true,
    sourcemap: "linked",
  });
  if (!result.success) {
    throw new Error(result.logs.map((log) => String(log)).join("\n"));
  }
  return result.outputs.map((artifact) => artifact.path.slice(outdir.length));
}

if (import.meta.main) {
  try {
    for (const path of await buildConsole()) console.log(path);
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exit(1);
  }
}
