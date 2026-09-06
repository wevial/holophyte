import tailwind from "bun-plugin-tailwind";

const outdir = new URL("./dist/", import.meta.url).pathname;

const result = await Bun.build({
  entrypoints: [new URL("./index.html", import.meta.url).pathname],
  outdir,
  plugins: [tailwind],
  minify: true,
  sourcemap: "linked",
});

if (!result.success) {
  for (const log of result.logs) console.error(log);
  process.exit(1);
}

for (const artifact of result.outputs) {
  console.log(`${artifact.path.slice(outdir.length)}  ${artifact.size} bytes`);
}
