import { Fragment } from "react";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { CONFIG_EDIT_OFF, type ConfigAnswer } from "../../lib/config";
import { hostName } from "../../lib/hosts";
import { storeScope, switcherRows } from "../projects";
import { useConfig } from "../queries";
import { keepScope, useConsolePeers } from "../scope";
import { hostsNeedingAttention, settingsHost, SETTINGS_FIELDS, SETTINGS_TABS } from "../settings";
import { SettingsDraftProvider, useSettingsDraft } from "../settingsDraft";
import { SettingRow } from "../components/SettingRow";
import { UnsavedBar } from "../components/UnsavedBar";
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

function SettingsPage({ tab }: { tab: Tab }) {
  const { hosts, now } = useConsolePeers();
  const search = useSearch({ from: "__root__" });
  const navigate = useNavigate();
  const rows = switcherRows(hosts, now, new Map()).filter((row) => row.name != null);
  const project = search.project ?? rows[0]?.name ?? "";
  const host = settingsHost(hosts, project);
  const { data: answer } = useConfig(host?.base ?? null);
  const file = answer?.state === "ok" ? answer.config.path.split("/").pop() || "config.toml" : "config.toml";
  const hostLabel = host ? hostName(host) : "its host";
  const attention = hostsNeedingAttention(hosts);
  const draft = useSettingsDraft();
  const edits = draft.projects[project] ?? {};
  const count = (tab: string) => SETTINGS_FIELDS.filter((field) => field.tab === tab && `${field.table}.${field.key}` in edits).length;
  return (
    <section aria-labelledby="page-title" className="max-w-[1180px] px-5 py-7 min-[720px]:px-8">
      <header className="border-b border-rule pb-5">
        <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Settings</h1>
        <p className="mt-2 break-words font-mono text-[13px] text-ink-2">{project} · {file} on {hostLabel}</p>
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
        <section className="border border-rule bg-paper p-5 min-[720px]:p-6">
          <header className="flex flex-wrap items-baseline gap-x-4 gap-y-2">
            <h2 className="font-sans text-[20px] font-semibold">{tab.label}</h2>
            <p className="break-words font-mono text-[13px] text-ink-3">{subtitle(tab.id, file, project)}</p>
          </header>
          {tab.id === "general" && host && answer?.state === "ok" && answer.config.values !== null ? (
            <div className="mt-6">
              {SETTINGS_FIELDS.filter((field) => field.tab === tab.id).map((field) => {
                const key = `${field.table}.${field.key}`;
                return <SettingRow key={key} field={field} values={answer.config.values} value={edits[key]?.value}
                  onChange={(value, original) => draft.edit(project, key, original, value)}
                  project={project} host={hostLabel} applies={answer.config.applies} />;
              })}
            </div>
          ) : <p className="mt-6 break-words font-mono text-[13px] text-ink-3">{panelLine(tab, answer, file, project, host ? hostLabel : null)}</p>}
        </section>
        <UnsavedBar project={project} />
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
