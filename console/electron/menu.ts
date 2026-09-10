/**
 * The tray menu as data. Pure: no Electron import at runtime, so the shape
 * of the menu is testable under `bun test`; `main.ts` turns the template
 * into a real menu with `Menu.buildFromTemplate` and supplies the clicks.
 */

export type MenuState = { openAtLogin: boolean };

export type MenuActions = {
  showConsole?: () => void;
  setOpenAtLogin?: (enabled: boolean) => void;
  toggleDevTools?: () => void;
  quit?: () => void;
};

/** The subset of Electron's `MenuItemConstructorOptions` the tray uses. */
export type TrayMenuItem = {
  label?: string;
  type?: "normal" | "separator" | "checkbox";
  checked?: boolean;
  enabled?: boolean;
  click?: (item: { checked: boolean }) => void;
};

export function menuTemplate(state: MenuState, actions: MenuActions = {}): TrayMenuItem[] {
  return [
    { label: "Show console", click: () => actions.showConsole?.() },
    {
      label: "Open at login",
      type: "checkbox",
      checked: state.openAtLogin,
      click: (item) => actions.setOpenAtLogin?.(item.checked),
    },
    { label: "Developer tools", click: () => actions.toggleDevTools?.() },
    { type: "separator" },
    { label: "Quit", click: () => actions.quit?.() },
  ];
}
