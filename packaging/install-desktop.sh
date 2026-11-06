#!/usr/bin/env bash
# Installs buddy as a desktop app for the current user (no sudo):
#   - app-grid launcher  ~/.local/share/applications/buddy.desktop
#   - icon               ~/.local/share/icons/hicolor/scalable/apps/buddy.svg
#   - with --autostart:  ~/.config/autostart/buddy.desktop (start at login)
#
# Usage: packaging/install-desktop.sh [--autostart] | --uninstall

set -euo pipefail

repository_directory="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
launcher_path="${repository_directory}/bin/buddy"
data_home="${XDG_DATA_HOME:-${HOME}/.local/share}"
config_home="${XDG_CONFIG_HOME:-${HOME}/.config}"
desktop_entry_path="${data_home}/applications/buddy.desktop"
icon_path="${data_home}/icons/hicolor/scalable/apps/buddy.svg"
autostart_entry_path="${config_home}/autostart/buddy.desktop"

refresh_caches() {
    update-desktop-database "${data_home}/applications" >/dev/null 2>&1 || true
    gtk-update-icon-cache -f -t "${data_home}/icons/hicolor" >/dev/null 2>&1 || true
}

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "${desktop_entry_path}" "${icon_path}" "${autostart_entry_path}"
    refresh_caches
    echo "Removed the buddy launcher, icon and autostart entry."
    exit 0
fi

if [ ! -x "${repository_directory}/.venv/bin/python" ]; then
    echo "No virtual environment at ${repository_directory}/.venv — create it first (README: 설치)." >&2
    exit 1
fi

chmod +x "${launcher_path}"
mkdir -p "$(dirname "${desktop_entry_path}")" "$(dirname "${icon_path}")"
install -m 644 "${repository_directory}/buddy/assets/buddy.svg" "${icon_path}"

write_desktop_entry() {
    # Exec is quoted so a repository path with spaces still works.
    cat >"$1" <<EOF
[Desktop Entry]
Type=Application
Name=buddy
GenericName=AI Screen Companion
Comment=Ask Claude about anything on your screen
Exec="${launcher_path}"
Icon=buddy
Terminal=false
Categories=Utility;
Keywords=claude;ai;assistant;screen;
StartupNotify=false
EOF
}

write_desktop_entry "${desktop_entry_path}"
echo "Installed launcher: ${desktop_entry_path}"

if [ "${1:-}" = "--autostart" ]; then
    mkdir -p "$(dirname "${autostart_entry_path}")"
    write_desktop_entry "${autostart_entry_path}"
    # Give the tray (AppIndicator) a moment to come up after login.
    echo "X-GNOME-Autostart-Delay=3" >>"${autostart_entry_path}"
    echo "Installed autostart:  ${autostart_entry_path}"
fi

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "${desktop_entry_path}"
fi
refresh_caches
echo "Done. Search for \"buddy\" in Activities (Super key), or right-click it there → Add to Favorites."
