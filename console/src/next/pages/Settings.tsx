import { Fragment, useState } from "react";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { CONFIG_EDIT_OFF, bind, namedKey, type ConfigProbe, type ConfigAnswer, type ConfigBody } from "../../lib/config";
import { hostName, underHost } from "../../lib/hosts";
import { RESTART_LABEL, ROUTES, ACTIONS_OFF } from "../../lib/actions";
import { RUN_SWEEP } from "../../lib/attention";
import { formatStamp } from "../../lib/format";
import { useAction, useConfigSave } from "../actions";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Btn } from "../components/Btn";
import { storeScope, switcherRows, type SwitcherRow } from "../projects";
import { useConfig } from "../queries";
import { keepScope, useConsolePeers } from "../scope";
import { hostsNeedingAttention, settingsHost, patchLines, SETTINGS_FIELDS, SETTINGS_TABS } from "../settings";
import { SettingsDraftProvider, useSettingsDraft } from "../settingsDraft";
import { SettingRow } from "../components/SettingRow";
import { UnsavedBar } from "../components/UnsavedBar";
import { AppearanceTab } from "../components/AppearanceTab";
import { HostsTab } from "../components/HostsTab";
import { NotFound } from "./Placeholder";

type Tab = (typeof SETTINGS_TABS)[number];

const GROUP_LABEL = "font-mono text-[11px] uppercase tracking-[0.1em] text-ink-3";

function subtitle(tab: Tab["id"], file: string, project: string): string {
  return {
    general: `[merge] and [loop] in ${file}`,
    review: `[loop], [review] and [agents] in ${file}`,
    agents: `[agents] in ${file}`,
    capture: `[merge] in ${file}`,
    hold: `admission for ${project} · applies now, no restart`,
    toml: file,
    hosts: "the factory's hosts",
    appearance: "yours, in this browser · applies at once, nothing to save",
  }[tab];
}

function panelLine(tab: Tab, answer: ConfigAnswer | undefined, file: string, project: string, host: string | null): string {
  if (tab.group !== "Project") return "This tab comes here.";
  if (host === null) return project ? `No host is available for ${project}.` : "No project host is available.";
  if (!answer) return `Reading ${file} for ${project} from ${host}…`;
  if (answer.state === "off") return CONFIG_EDIT_OFF;
  if (answer.state === "error") return `${host} did not answer /config: ${answer.error}`;
  if (answer.state === "needs_token") return `${host} needs its serve token to read /config.`;
  if (answer.config.values === null) return "The file does not parse as TOML. Settings cannot be edited until the file is fixed.";
  return "This tab comes here.";
}

function configStamp(config: ConfigBody): string {
  const revision = config.revision ? ` · revision ${config.revision.slice(0, 7)}` : "";
  const { saved, modified_ms: modified } = config;
  if (modified != null && (!saved || modified > saved.at_ms + 5000)) return `${revision} · changed on the host ${formatStamp(modified)}`;
  if (!saved) return revision;
  const actor = saved.actor === "maintainer" ? "you" : saved.actor === "operator" ? "the operator seat" : saved.actor === "supervisor" ? "the supervisor" : null;
  return `${revision} · saved ${formatStamp(saved.at_ms)}${actor ? ` by ${actor}` : ""}`;
}

function SettingsPage({ tab }: { tab: Tab }) {
  const { hosts, now } = useConsolePeers();
  const search = useSearch({ from: "__root__" });
  const rows = switcherRows(hosts, now, new Map()).filter((row) => row.name != null);
  const project = search.project ?? rows[0]?.name ?? "";
  return <SettingsProjectPage key={project} project={project} tab={tab} rows={rows} />;
}

function SettingsProjectPage({ tab, project, rows }: { tab: Tab; project: string; rows: SwitcherRow[] }) {
  const { hosts } = useConsolePeers();
  const navigate = useNavigate();
  const host = settingsHost(hosts, project);
  const { data: answer } = useConfig(host?.base ?? null);
  const file = answer?.state === "ok" ? answer.config.path.split("/").pop() || "config.toml" : "config.toml";
  const hostLabel = host ? hostName(host) : "its host";
  const attention = hostsNeedingAttention(hosts);
  const draft = useSettingsDraft();
  const edits = draft.projects[project] ?? {};
  const save = useConfigSave();
  const restart = useAction();
  const [confirm, setConfirm] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [serverErrors, setErrors] = useState<Record<string, string>>({});
  const [banner, setBanner] = useState<{ revision: string; applies: string; probe?: ConfigProbe | null } | null>(null);
  const values = answer?.state === "ok" ? answer.config.values : null;
  const effective = (key: string) => {
    if (edits[key]) return edits[key].value;
    const field = SETTINGS_FIELDS.find((field) => `${field.table}.${field.key}` === key)!;
    const bound = bind(field, values);
    return bound.state === "bound" ? bound.value : bound.state === "absent" ? field.default : undefined;
  };
  const errors = { ...serverErrors };
  const rounds = effective("loop.review_rounds");
  const cap = effective("loop.review_rounds_max");
  if (typeof rounds === "number" && typeof cap === "number" && rounds > cap) {
    errors["loop.review_rounds"] = `Above the round cap of ${cap}. Raise the cap or lower this.`;
  }
  for (const field of SETTINGS_FIELDS.filter((field) => field.tab === "agents")) {
    const key = `${field.table}.${field.key}`;
    const value = edits[key]?.value;
    if (typeof value === "string" && !value.trim()) errors[key] = "A seat needs a command. Remove the key in Raw TOML to use the default.";
  }
  const canSave = host?.status?.config_edit === true && answer?.state === "ok" && answer.config.values !== null && Object.keys(errors).length === 0;
  const restartLabel = host && underHost(host.base) ? RUN_SWEEP : RESTART_LABEL;
  const write = async () => {
    if (!canSave || !host || answer?.state !== "ok" || save.isPending) return;
    setRefusal(null);
    const result = await save.mutateAsync({ base: host.base, revision: answer.config.revision, patch: Object.fromEntries(Object.entries(edits).map(([key, edit]) => [key, edit.value])) });
    if (result.ok) {
      draft.saved(project, edits);
      setConfirm(false);
      setBanner({ ...result, probe: edits["agents.implementer"] ? result.probe : undefined });
    } else if ("conflict" in result) {
      setRefusal(`config.toml changed on ${hostLabel} since this page read it. It has been read again: check the fields and save once more.`);
    } else {
      const key = result.refused ? namedKey(result.error) : null;
      const field = SETTINGS_FIELDS.find((field) => `${field.table}.${field.key}` === key);
      if (key && field) {
        setErrors((previous) => ({ ...previous, [key]: result.error }));
        setConfirm(false);
        void navigate({ to: "/settings/$tab", params: { tab: field.tab }, search: { project } });
      } else setRefusal(result.error);
    }
  };
  const count = (tab: string) => SETTINGS_FIELDS.filter((field) => field.tab === tab && `${field.table}.${field.key}` in edits).length;
  return (
    <section aria-labelledby="page-title" className="max-w-[1180px] px-5 py-7 min-[720px]:px-8">
      <header className="border-b border-rule pb-5">
        <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Settings</h1>
        <p className="mt-2 break-words font-mono text-[13px] text-ink-2">{project} · {file} on {hostLabel}{answer?.state === "ok" ? configStamp(answer.config) : ""}</p>
      </header>
      <div className="flex min-w-0 flex-col gap-6 py-6 min-[720px]:flex-row min-[720px]:gap-8">
        <nav aria-label="Settings sections" className="flex shrink-0 flex-wrap items-start gap-y-1 min-[720px]:w-[212px] min-[720px]:flex-col min-[720px]:items-stretch">
          <div className="mb-3 w-full">
            <label htmlFor="settings-project" className={GROUP_LABEL}>Project</label>
            <select
              id="settings-project"
              value={project}
              onChange={(event) => {
                const project = event.target.value;
                storeScope(project);
                void navigate({ to: "/settings/$tab", params: { tab: tab.id }, search: { project } });
              }}
              className="mt-2 min-h-10 w-full min-w-0 border border-rule bg-paper px-3 font-mono text-[13px] text-ink"
            >
              {rows.map((row) => <option key={row.name} value={row.name!}>{row.label}{row.held ? " · held" : ""}</option>)}
            </select>
          </div>
          {SETTINGS_TABS.map((item, index) => (
            <Fragment key={item.id}>
              {item.group !== "Project" && SETTINGS_TABS[index - 1]?.group !== item.group && <p className={`${GROUP_LABEL} mb-2 mt-5 w-full px-3`}>{item.group}</p>}
              <Link
                to="/settings/$tab"
                params={{ tab: item.id }}
                search={keepScope}
                aria-current={tab.id === item.id ? "page" : undefined}
                className={`flex min-h-10 items-center gap-3 border-l-2 px-3 py-2 text-[14px] ${tab.id === item.id ? "border-act bg-act-wash text-act" : "border-transparent text-ink-2 hover:bg-paper-2 hover:text-ink"}`}
              >
                {item.label}
                {item.group === "Project" && count(item.id) > 0 && <span className="ml-auto font-mono text-[11px] text-act">{count(item.id)}</span>}
                {item.id === "hold" && host?.status?.admission === "held" && <span className="font-mono text-[11px] text-wait">held</span>}
                {item.id === "hosts" && attention > 0 && <span className="ml-auto font-mono text-[12px] text-need">{attention}</span>}
              </Link>
            </Fragment>
          ))}
        </nav>
        <div className="min-w-0 flex-1">
        {banner && <div role="status" className="mb-4 border border-live bg-paper p-5">
          <p className="font-mono text-[13px] text-live">Saved config.toml at revision {banner.revision.slice(0, 7)}.</p>
          <p className="mt-2 font-read text-[14px] text-ink-2">It applies at the {banner.applies}.</p>
          {banner.probe && <p className={`mt-2 font-mono text-[13px] ${banner.probe.ok ? "text-live" : "text-bad"}`}>{banner.probe.ok ? "Implementer probe passed." : `Implementer probe failed: ${banner.probe.output.at(-1) ?? ""}`}</p>}
          <div className="mt-4 flex gap-2">
            <Btn variant="primary" disabled={host?.status?.actions !== true} title={host?.status?.actions !== true ? ACTIONS_OFF : undefined} busy={restart.isPending} busyLabel="Restarting…" onClick={() => host && restart.mutate({ label: restartLabel, base: host.base, route: ROUTES[restartLabel]!, body: {}, said: "Restart request accepted." })}>{restartLabel}</Btn>
            <Btn onClick={() => setBanner(null)}>Later</Btn>
          </div>
          {restart.data && <p className="mt-3 font-mono text-[12px]">{restart.data.detail}</p>}
        </div>}
        <section className="border border-rule bg-paper p-5 min-[720px]:p-6">
          <header className="flex flex-wrap items-baseline gap-x-4 gap-y-2">
            <h2 className="font-sans text-[20px] font-semibold">{tab.label}</h2>
            <p className="break-words font-mono text-[13px] text-ink-3">{subtitle(tab.id, file, project)}</p>
          </header>
          {["general", "review", "agents", "capture"].includes(tab.id) && host && answer?.state === "ok" && answer.config.values !== null ? (
            <div className="mt-6">
              {SETTINGS_FIELDS.filter((field) => field.tab === tab.id).map((field) => {
                const key = `${field.table}.${field.key}`;
                return <SettingRow key={key} field={field} values={answer.config.values} value={edits[key]?.value}
                  status={host.status ?? undefined} disabled={key === "merge.ui_capture_video" && !effective("merge.ui_capture")}
                  error={errors[key]} onChange={(value, original) => {
                    setErrors(({ [key]: _cleared, ...rest }) => rest);
                    draft.edit(project, key, original, value);
                  }}
                  project={project} host={hostLabel} applies={answer.config.applies} />;
              })}
            </div>
          ) : tab.id === "appearance" ? <AppearanceTab /> : tab.id === "hosts" ? <HostsTab /> : <p className="mt-6 break-words font-mono text-[13px] text-ink-3">{panelLine(tab, answer, file, project, host ? hostLabel : null)}</p>}
        </section>
        <UnsavedBar invalid={Object.keys(errors).length > 0} project={project} onDiscard={() => setErrors({})} disabled={!canSave || save.isPending} onSave={() => { setRefusal(null); setConfirm(true); }} />
        {confirm && <ConfirmDialog label="Save · holophyte" heading="Save these changes?" confirm="Save" busyLabel="Saving…" busy={save.isPending} confirmDisabled={!canSave} cancelLabel="Keep editing" onCancel={() => { if (!save.isPending) setConfirm(false); }} onConfirm={() => void write()}
          refusal={refusal && <p role="alert" className="mt-4 font-mono text-[13px] text-bad">{refusal}</p>}>
          <p className="mt-4 font-read text-[15px] text-ink-2">These lines change in config.toml on {hostLabel}.</p>
          <div className="my-5 border border-rule py-2 font-mono text-[13px]">
            {patchLines(answer?.state === "ok" ? answer.config.values : null, edits).map((line, index) => <div key={index} className={`whitespace-pre-wrap break-words px-3 py-1 ${line.kind === "removed" ? "bg-bad-wash text-bad" : line.kind === "added" ? "text-live" : "text-ink-3"}`}>{line.kind === "removed" ? "− " : line.kind === "added" ? "+ " : ""}{line.text}</div>)}
          </div>
          <p className="font-read text-[14px] text-ink-2">Applies at the {answer?.state === "ok" ? answer.config.applies : "next loop start"}.</p>
          <p className="mt-2 font-read text-[14px] text-ink-3">Runs in flight keep the settings they started with.</p>
        </ConfirmDialog>}
        </div>
      </div>
      <footer className="flex flex-wrap gap-x-6 gap-y-2 border-t border-rule pt-4 font-mono text-[12px] text-ink-3">
        <p>project settings live in {file} on its host; each save keeps the previous text as a backup</p>
        <p>appearance stays in this browser</p>
      </footer>
    </section>
  );
}

export function Settings() {
  const { tab: id = "general" } = useParams({ strict: false });
  const tab = SETTINGS_TABS.find((tab) => tab.id === id);
  return <SettingsDraftProvider>{tab ? <SettingsPage tab={tab} /> : <NotFound />}</SettingsDraftProvider>;
}
