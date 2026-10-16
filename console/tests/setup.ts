import { GlobalRegistrator } from "@happy-dom/global-registrator";

// bun test runs in UTC without setting TZ, and deleting TZ keeps the last zone set, so a file that restores an unset TZ needs it named.
process.env.TZ ??= "UTC";

GlobalRegistrator.register();
