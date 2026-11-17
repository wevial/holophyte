import { afterAll, afterEach, beforeAll, beforeEach, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import type { Status } from "../../src/lib/types";
import { fakeDeps, NO_ATTENTION, settle, stubFetch } from "../harness";
import { declarations } from "../cssTokens";
import { oklchToHex } from "../../src/lib/oklch";
import * as haze from "../../src/vendor/spectra/haze";

const ORIGIN = "http://writer-host:7710";
const status = await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json() as Status;
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
const root = document.documentElement;

async function mount() {
  window.location.hash = "#/settings/appearance";
  const fake = fakeDeps(stubFetch({ status, attention: NO_ATTENTION }));
  render(<QueryClientProvider client={consoleQueryClient()}><ConsoleApp base={ORIGIN} pollDeps={fake.deps} /></QueryClientProvider>);
  await act(settle);
  await act(settle);
}

function radio(group: string, name: string) {
  return within(screen.getByRole("radiogroup", { name: group })).getByRole("radio", { name: new RegExp(`^${name}\\b`, "i") }) as HTMLInputElement;
}

const scheme = (name: string) => within(screen.getByRole("radiogroup", { name: "Theme" })).getByRole("radio", { name });
const tile = (row: string, name: string) => within(screen.getByRole("group", { name: row })).getByRole("button", { name: new RegExp(`^${name}`) });
const pressed = (row: string) => within(screen.getByRole("group", { name: row })).getAllByRole("button").filter((button) => button.getAttribute("aria-pressed") === "true").map((button) => button.textContent);
const slider = (name: string) => screen.getByRole("slider", { name }) as HTMLInputElement;
const motion = () => screen.getByRole("switch", { name: "Motion" });
const shell = () => screen.getByRole("main").parentElement!;
const KEYS = ["holophyte.theme", "holophyte.theme.light", "holophyte.theme.dark", "holophyte.next.effect", "holophyte.next.motion", "holophyte.textSize", "holophyte.grain", "holophyte.hazeStrength", "holophyte.next.font", "holophyte.next.density"];
const stored = () => KEYS.filter((key) => localStorage.getItem(key) !== null);
const FONT_NAMES = ["Plex Sans", "Plex Serif", "Hyperlegible"];
const storedValues = () => Object.fromEntries(KEYS.map((key) => [key, localStorage.getItem(key)]));
const controls = () => ({
  scheme: within(screen.getByRole("radiogroup", { name: "Theme" })).getAllByRole("radio").filter((radio) => radio.getAttribute("aria-checked") === "true").map((radio) => radio.textContent),
  light: pressed("Light"), dark: pressed("Dark"), header: pressed("Header"),
  motion: motion().getAttribute("aria-checked"),
  sliders: [slider("Text size").value, slider("Grain").value, slider("Haze strength").value],
  font: FONT_NAMES.filter((name) => radio("Reading font", name).checked),
  density: screen.getAllByRole("button", { name: /^(Comfortable|Compact)$/ }).filter((button) => button.getAttribute("aria-pressed") === "true").map((button) => button.textContent),
});

function reducedMotion(reduced: boolean) {
  return spyOn(window, "matchMedia").mockImplementation((media: string) => ({
    matches: media === "(prefers-reduced-motion: reduce)" ? reduced : false,
    media,
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as MediaQueryList);
}

const sidebarTheme = (name: string) => within(screen.getByRole("group", { name: "Theme" })).getByRole("button", { name });
const sidebarPressed = () =>
  within(screen.getByRole("group", { name: "Theme" })).getAllByRole("button").filter((button) => button.getAttribute("aria-pressed") === "true").map((button) => button.getAttribute("aria-label"));

beforeAll(() => happyDOM.setURL(`${ORIGIN}/`));
afterAll(() => happyDOM.setURL("about:blank"));
function clearRoot() {
  for (const name of ["data-theme", "data-light", "data-dark", "data-font", "data-density", "data-text-size", "data-text-floor", "style"]) root.removeAttribute(name);
  root.style.cssText = "";
}

beforeEach(() => {
  localStorage.clear();
  clearRoot();
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
  localStorage.clear();
  clearRoot();
});

test("with nothing stored, System, Lotus in both rows, Haze and Motion are on and every slider reads 100%", async () => {
  await mount();
  expect(scheme("System").getAttribute("aria-checked")).toBe("true");
  expect(pressed("Light")).toEqual(["Lotus"]);
  expect(pressed("Dark")).toEqual(["Lotus"]);
  expect(pressed("Header")).toEqual(["Haze"]);
  expect(motion().getAttribute("aria-checked")).toBe("true");
  expect([slider("Text size").value, slider("Grain").value, slider("Haze strength").value]).toEqual(["100", "100", "100"]);
  expect(screen.getAllByText("100%")).toHaveLength(3);
});

test("Tide in the Dark row and Dune in the Light row are pressed, stored and carried by the document, the canvas resolving to Tide dark under Dark and Dune light under Light", async () => {
  const style = document.createElement("style");
  style.textContent = (await Bun.file(new URL("../../src/app/theme.css", import.meta.url)).text()).replace(/@import[^;]*;/, "");
  try {
    await mount();
    fireEvent.click(tile("Dark", "Tide"));
    fireEvent.click(tile("Light", "Dune"));
    fireEvent.click(scheme("Dark"));
    expect(pressed("Dark")).toEqual(["Tide"]);
    expect(pressed("Light")).toEqual(["Dune"]);
    expect([localStorage.getItem("holophyte.theme.dark"), localStorage.getItem("holophyte.theme.light"), localStorage.getItem("holophyte.theme")]).toEqual(["tide", "dune", "dark"]);
    expect([root.getAttribute("data-dark"), root.getAttribute("data-light")]).toEqual(["tide", "dune"]);
    document.head.appendChild(style);
    expect(getComputedStyle(root).getPropertyValue("--canvas").trim()).toBe("#0b0e1d");
    expect(sidebarPressed()).toEqual(["Night"]);
    fireEvent.click(scheme("Light"));
    expect(getComputedStyle(root).getPropertyValue("--canvas").trim()).toBe("#fcfaf7");
  } finally {
    style.remove();
  }
});

test("Caustics is disabled, and picking None stores none and removes the shell's haze band", async () => {
  await mount();
  expect((tile("Header", "Caustics") as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByRole("main").querySelectorAll("[data-haze]")).toHaveLength(1);
  fireEvent.click(tile("Header", "None"));
  expect(pressed("Header")).toEqual(["None"]);
  expect(localStorage.getItem("holophyte.next.effect")).toBe("none");
  expect(screen.getByRole("main").querySelector("[data-haze]")).toBeNull();
});

test("under prefers-reduced-motion the Motion switch is off and disabled", async () => {
  const media = reducedMotion(true);
  try {
    await mount();
    expect(motion().getAttribute("aria-checked")).toBe("false");
    expect((motion() as HTMLButtonElement).disabled).toBe(true);
  } finally {
    cleanup();
    media.mockRestore();
  }
});

test("text at 110% is stored, zooms the document around the shell by 1.1 and shows the Text reset, which returns 100% and hides", async () => {
  await mount();
  expect(screen.queryByRole("button", { name: "Reset text size" })).toBeNull();
  fireEvent.change(slider("Text size"), { target: { value: "110" } });
  expect(localStorage.getItem("holophyte.textSize")).toBe("110");
  expect(root.contains(shell())).toBe(true);
  expect(root.style.getPropertyValue("zoom")).toBe("1.1");
  fireEvent.click(screen.getByRole("button", { name: "Reset text size" }));
  expect(slider("Text size").value).toBe("100");
  expect(localStorage.getItem("holophyte.textSize")).toBeNull();
  expect(root.style.getPropertyValue("zoom")).toBe("");
  expect(screen.queryByRole("button", { name: "Reset text size" })).toBeNull();
});

test("grain at 50% and haze at 150% set the document's scales, and each row's reset restores 100%", async () => {
  await mount();
  fireEvent.change(slider("Grain"), { target: { value: "50" } });
  fireEvent.change(slider("Haze strength"), { target: { value: "150" } });
  expect([root.style.getPropertyValue("--grain-scale"), root.style.getPropertyValue("--haze-scale")]).toEqual(["0.5", "1.5"]);
  fireEvent.click(screen.getByRole("button", { name: "Reset grain" }));
  fireEvent.click(screen.getByRole("button", { name: "Reset haze strength" }));
  expect([slider("Grain").value, slider("Haze strength").value]).toEqual(["100", "100"]);
  expect([root.style.getPropertyValue("--grain-scale"), root.style.getPropertyValue("--haze-scale")]).toEqual(["", ""]);
});

test("Reset to default asks inline: Cancel keeps every choice and Confirm restores every default and clears the stored keys", async () => {
  await mount();
  fireEvent.click(scheme("Dark"));
  fireEvent.click(tile("Dark", "Tide"));
  fireEvent.click(tile("Light", "Dune"));
  fireEvent.click(tile("Header", "None"));
  fireEvent.click(motion());
  fireEvent.change(slider("Text size"), { target: { value: "110" } });
  fireEvent.change(slider("Grain"), { target: { value: "50" } });
  fireEvent.change(slider("Haze strength"), { target: { value: "150" } });
  fireEvent.click(radio("Reading font", "Hyperlegible"));
  fireEvent.click(screen.getByRole("button", { name: "Compact" }));
  expect(stored()).toEqual(KEYS);
  const values = storedValues();
  const before = controls();

  fireEvent.click(screen.getByRole("button", { name: "Reset to default" }));
  expect(screen.getByText("Reset?")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Reset to default" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByText("Reset?")).toBeNull();
  expect(storedValues()).toEqual(values);
  expect(controls()).toEqual(before);
  expect(before).toEqual({ scheme: ["Dark"], light: ["Dune"], dark: ["Tide"], header: ["None"], motion: "false", sliders: ["110", "50", "150"], font: ["Hyperlegible"], density: ["Compact"] });
  expect([root.getAttribute("data-font"), root.getAttribute("data-density")]).toEqual(["hyperlegible", "compact"]);
  expect([root.style.getPropertyValue("zoom"), root.style.getPropertyValue("--grain-scale"), root.style.getPropertyValue("--haze-scale")]).toEqual(["1.1", "0.5", "1.5"]);

  fireEvent.click(screen.getByRole("button", { name: "Reset to default" }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
  expect(stored()).toEqual([]);
  expect(scheme("System").getAttribute("aria-checked")).toBe("true");
  expect([pressed("Light"), pressed("Dark"), pressed("Header")]).toEqual([["Lotus"], ["Lotus"], ["Haze"]]);
  expect(motion().getAttribute("aria-checked")).toBe("true");
  expect([slider("Text size").value, slider("Grain").value, slider("Haze strength").value]).toEqual(["100", "100", "100"]);
  expect(["data-theme", "data-light", "data-dark"].filter((name) => root.hasAttribute(name))).toEqual([]);
  expect(root.style.getPropertyValue("--grain-scale")).toBe("");
  expect(screen.getByRole("main").querySelectorAll("[data-haze]")).toHaveLength(1);
  expect(screen.getByRole("button", { name: "Reset to default" })).toBeTruthy();
});

test("Reset to default also returns Reading font and Density to their defaults", async () => {
  await mount();
  fireEvent.click(radio("Reading font", "Plex Serif"));
  fireEvent.click(screen.getByRole("button", { name: "Compact" }));
  expect([root.getAttribute("data-font"), root.getAttribute("data-density")]).toEqual(["serif", "compact"]);
  fireEvent.click(screen.getByRole("button", { name: "Reset to default" }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
  expect(radio("Reading font", "Plex Sans").checked).toBe(true);
  expect(screen.getByRole("button", { name: "Comfortable" }).getAttribute("aria-pressed")).toBe("true");
  expect([root.hasAttribute("data-font"), root.hasAttribute("data-density")]).toEqual([false, false]);
  expect(stored()).toEqual([]);
});

test("the Appearance scheme switch and the sidebar share one theme store", async () => {
  await mount();
  fireEvent.click(scheme("Light"));
  expect(root.getAttribute("data-theme")).toBe("light");
  expect(sidebarPressed()).toEqual(["Dawn"]);
  fireEvent.click(sidebarTheme("Night"));
  expect(scheme("Dark").getAttribute("aria-checked")).toBe("true");
  fireEvent.click(sidebarTheme("System"));
  expect(scheme("System").getAttribute("aria-checked")).toBe("true");
  expect(localStorage.getItem("holophyte.theme")).toBeNull();
});

test("Density is a group of Comfortable and Compact; Compact presses and Reset density returns Comfortable", async () => {
  await mount();
  const density = screen.getByRole("group", { name: "Density" });
  expect(within(density).getAllByRole("button").map((button) => button.textContent)).toEqual(["Comfortable", "Compact"]);
  expect(pressed("Density")).toEqual(["Comfortable"]);
  expect(density.getAttribute("aria-describedby")).toBe("appearance-density-help");
  expect(document.getElementById("appearance-density-help")!.textContent).toMatch(/^Compact tightens rows/);
  fireEvent.click(within(density).getByRole("button", { name: "Compact" }));
  expect(pressed("Density")).toEqual(["Compact"]);
  fireEvent.click(screen.getByRole("button", { name: "Reset density" }));
  expect(pressed("Density")).toEqual(["Comfortable"]);
  expect(screen.queryByRole("button", { name: "Reset density" })).toBeNull();
});

test("the Density preview is a region named Preview holding its two sample rows with no visible Preview caption", async () => {
  await mount();
  const preview = screen.getByRole("region", { name: "Preview" });
  expect(Array.from(preview.querySelectorAll("[data-row]"), (row) => row.textContent)).toEqual(Array(2).fill("reviewingHOLO-231The sweep frees a silent lease16m / 25m"));
  expect(screen.queryAllByText(/^Preview$/i)).toEqual([]);
});

test("reading font choices persist in this browser and Plex Sans removes the override", async () => {
  await mount();
  expect(within(screen.getByRole("radiogroup", { name: "Reading font" })).getAllByRole("radio").map((input) => input.getAttribute("aria-label"))).toEqual(FONT_NAMES);
  expect(radio("Reading font", "Plex Sans").checked).toBe(true);
  fireEvent.click(radio("Reading font", "Plex Serif"));
  expect(root.getAttribute("data-font")).toBe("serif");
  expect(localStorage.getItem("holophyte.next.font")).toBe("serif");
  cleanup();
  await mount();
  expect(radio("Reading font", "Plex Serif").checked).toBe(true);
  fireEvent.click(radio("Reading font", "Hyperlegible"));
  expect(root.getAttribute("data-font")).toBe("hyperlegible");
  expect(localStorage.getItem("holophyte.next.font")).toBe("hyperlegible");
  fireEvent.click(radio("Reading font", "Plex Sans"));
  expect(root.hasAttribute("data-font")).toBe(false);
  expect(localStorage.getItem("holophyte.next.font")).toBeNull();
});

test("blocked browser storage still applies reading font and shared theme choices", async () => {
  const blocked = () => { throw new Error("Storage is disabled"); };
  const access = [
    spyOn(Storage.prototype, "getItem").mockImplementation(blocked),
    spyOn(Storage.prototype, "setItem").mockImplementation(blocked),
    spyOn(Storage.prototype, "removeItem").mockImplementation(blocked),
  ];
  try {
    await mount();
    fireEvent.click(radio("Reading font", "Plex Serif"));
    expect(root.getAttribute("data-font")).toBe("serif");
    expect(radio("Reading font", "Plex Serif").checked).toBe(true);
    fireEvent.click(radio("Reading font", "Plex Sans"));
    expect(root.hasAttribute("data-font")).toBe(false);
    expect(radio("Reading font", "Plex Sans").checked).toBe(true);
    fireEvent.click(scheme("Light"));
    expect(root.getAttribute("data-theme")).toBe("light");
    fireEvent.click(sidebarTheme("Night"));
    expect(scheme("Dark").getAttribute("aria-checked")).toBe("true");
  } finally {
    cleanup();
    for (const mock of access) mock.mockRestore();
  }
});

test("font attributes select the promised reading stacks", async () => {
  const css = declarations(await Bun.file(new URL("../../src/app/theme.css", import.meta.url)).text());
  const font = (selector: string) => css.find((entry) => entry.path.length === 1 && entry.path[0] === selector && entry.name === "--font-read")?.value;
  expect(font(':root[data-font="serif"]')).toBe('"IBM Plex Serif", Georgia, serif');
  expect(font(':root[data-font="hyperlegible"]')).toBe('"Atkinson Hyperlegible Next", "IBM Plex Sans", Arial, sans-serif');
});

const editor = () => within(screen.getByRole("region", { name: "Theme editor" }));
const nameField = () => editor().getByRole("textbox", { name: "Theme name" }) as HTMLInputElement;
const flag = (name: string) => within(screen.getByRole("list", { name: "Contrast" })).getByRole("listitem", { name: new RegExp(`^${name} \\d`) });
const isDisabled = (control: HTMLElement) => (control as HTMLInputElement).disabled || control.getAttribute("aria-disabled") === "true";

/** The square spans chroma 0 to 0.1 left to right and lightness 1 to 0 top to bottom. */
function pickGround(l: number, c: number, h: number) {
  const thumb = editor().getByRole("slider", { name: "Ground lightness and chroma" });
  const square = thumb.parentElement!;
  square.getBoundingClientRect = () => ({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
  fireEvent.pointerDown(square, { clientX: c * 1000, clientY: (1 - l) * 100, pointerId: 1 });
  fireEvent.change(editor().getByRole("slider", { name: "Ground hue" }), { target: { value: String(h) } });
  expect(thumb.getAttribute("aria-valuetext")).toBe(`Lightness ${l.toFixed(2)}, chroma ${c.toFixed(3)}`);
}

async function saveDarkTheme() {
  await mount();
  fireEvent.click(scheme("Dark"));
  fireEvent.click(within(screen.getByRole("group", { name: "Dark" })).getByRole("button", { name: /New theme/ }));
  fireEvent.change(nameField(), { target: { value: "Plum night" } });
  pickGround(0.2, 0.05, 330);
  fireEvent.click(editor().getByRole("button", { name: "Save" }));
}

test("a mid-grey light ground raises the Muted text flag with its ratio under 4.5 while Text passes", async () => {
  await mount();
  fireEvent.click(within(screen.getByRole("group", { name: "Light" })).getByRole("button", { name: /New theme/ }));
  pickGround(0.62, 0.01, 250);
  const muted = flag("Muted text");
  const ratio = Number(muted.getAttribute("title")!.match(/^Muted text (\d+\.\d):1 · under 4\.5$/)![1]);
  expect(ratio).toBeLessThan(4.5);
  expect(muted.hasAttribute("data-fails")).toBe(true);
  expect(flag("Text").hasAttribute("data-fails")).toBe(false);
  expect(flag("Text").getAttribute("title")).not.toContain("under");
});

test("Lotus dark in the editor is read-only, and Edit a copy opens an editable Lotus dark copy", async () => {
  await mount();
  fireEvent.click(tile("Dark", "Lotus"));
  expect(nameField().value).toBe("Lotus dark");
  const controls = [nameField(), ...editor().getAllByRole("slider"), ...within(editor().getByRole("group", { name: "Haze colours" })).getAllByRole("button")];
  expect(controls.length).toBeGreaterThan(5);
  expect(controls.filter((control) => !isDisabled(control))).toEqual([]);
  expect(editor().queryByRole("button", { name: "Save" })).toBeNull();
  fireEvent.click(editor().getByRole("button", { name: "Edit a copy" }));
  expect(nameField().value).toBe("Lotus dark copy");
  expect(nameField().disabled).toBe(false);
  expect(editor().getAllByRole("slider").filter(isDisabled)).toEqual([]);
});

test("Edit a copy opens a copy where crypto.randomUUID is unavailable, as outside a secure context", async () => {
  const randomUUID = spyOn(crypto, "randomUUID").mockImplementation(() => { throw new TypeError("crypto.randomUUID is not a function"); });
  try {
    await mount();
    fireEvent.click(tile("Dark", "Lotus"));
    fireEvent.click(editor().getByRole("button", { name: "Edit a copy" }));
    expect(nameField().value).toBe("Lotus dark copy");
    expect(nameField().disabled).toBe(false);
  } finally {
    randomUUID.mockRestore();
  }
});

test("a saved dark custom theme is marked custom and pressed in the Dark row, survives a remount and sets the document's canvas to its ground", async () => {
  await saveDarkTheme();
  const canvas = oklchToHex({ l: 0.2, c: 0.05, h: 330 });
  expect(pressed("Dark")).toEqual(["Plum night custom"]);
  expect(root.style.getPropertyValue("--canvas")).toBe(canvas);
  cleanup();
  await mount();
  expect(pressed("Dark")).toEqual(["Plum night custom"]);
  expect(root.style.getPropertyValue("--canvas")).toBe(canvas);
  fireEvent.click(scheme("Light"));
  expect(root.style.getPropertyValue("--canvas")).toBe("");
});

test("deleting the picked custom theme through its confirm removes its tile and returns the dark pick to Lotus", async () => {
  await saveDarkTheme();
  fireEvent.click(editor().getByRole("button", { name: "Delete" }));
  fireEvent.click(within(editor().getByRole("group", { name: "Delete?" })).getByRole("button", { name: "Delete" }));
  expect(within(screen.getByRole("group", { name: "Dark" })).queryByRole("button", { name: /^Plum night/ })).toBeNull();
  expect(pressed("Dark")).toEqual(["Lotus"]);
  expect(localStorage.getItem("holophyte.theme.dark")).toBeNull();
  expect(root.style.getPropertyValue("--canvas")).toBe("");
});

test("re-saving the picked custom theme with new haze colours hands the header renderer a changed config naming those colours", async () => {
  const configs: Record<string, unknown>[] = [];
  const renderer = spyOn(haze, "mount").mockImplementation((container: HTMLElement, options = {}) => {
    container.appendChild(document.createElement("canvas"));
    const config = { ...options } as Record<string, unknown>;
    configs.push(config);
    return { config, destroy: () => {} } as unknown as ReturnType<typeof haze.mount>;
  });
  try {
    await saveDarkTheme();
    const config = configs.at(-1)!;
    const before = String(config.palette);
    fireEvent.change(editor().getByRole("slider", { name: "Haze back hue" }), { target: { value: "120" } });
    fireEvent.click(editor().getByRole("button", { name: "Save" }));
    expect(String(config.palette)).not.toBe(before);
    expect(haze.PALETTES[String(config.palette)]![1]).toBe(root.style.getPropertyValue("--haze-back"));
  } finally {
    cleanup();
    renderer.mockRestore();
  }
});
