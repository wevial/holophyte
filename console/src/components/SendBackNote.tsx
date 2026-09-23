import type { RowDaemon } from "./RowActions";
import { ReasonAction } from "./ReasonAction";

/** Private maintainer feedback; only the daemon receives this text. */
export function SendBackNote({ daemon, runId }: { daemon: RowDaemon; runId: number }) {
  return <ReasonAction daemon={daemon} route="/actions/send-back" body={{ run: runId }}
    label="Send back with note" boxLabel="Maintainer's note" />;
}
