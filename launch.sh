#!/bin/bash
# launch.sh — GUI sudo prompt launcher
# Finds and launches the Python script in the same directory as this launcher.
# If multiple .py files exist, prompts you to pick one.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Pick a GUI dialog tool ────────────────────────────────────────────────────
if command -v zenity &>/dev/null; then
    DIALOG="zenity"
elif command -v kdialog &>/dev/null; then
    DIALOG="kdialog"
else
    MSG="zenity or kdialog is required.\nInstall with: sudo apt install zenity"
    command -v xmessage &>/dev/null && xmessage -center "$MSG"
    exit 1
fi

# ── Helpers ───────────────────────────────────────────────────────────────────
show_error() {
    if [ "$DIALOG" = "zenity" ]; then
        zenity --error --title="Launcher" --text="$1" --no-wrap 2>/dev/null
    else
        kdialog --error "$1" --title "Launcher" 2>/dev/null
    fi
}

get_password() {
    if [ "$DIALOG" = "zenity" ]; then
        zenity --password --title="sudo required" \
               --text="Enter your sudo password:" 2>/dev/null
    else
        kdialog --password "Enter your sudo password:" \
                --title "sudo required" 2>/dev/null
    fi
}

pick_script() {
    # $@ = list of .py files — let user pick one
    if [ "$DIALOG" = "zenity" ]; then
        zenity --list --title="Select script to launch" \
               --column="Python script" "$@" 2>/dev/null
    else
        kdialog --menu "Select script to launch" \
               $(for f in "$@"; do echo "$f" "$f"; done) 2>/dev/null
    fi
}

# ── Find Python scripts in the same directory ─────────────────────────────────
mapfile -t PY_FILES < <(find "$SCRIPT_DIR" -maxdepth 1 -name "*.py" | sort)

if [ ${#PY_FILES[@]} -eq 0 ]; then
    show_error "No Python scripts found in:\n$SCRIPT_DIR"
    exit 1
elif [ ${#PY_FILES[@]} -eq 1 ]; then
    PYTHON_FILE="${PY_FILES[0]}"
else
    # Multiple scripts — show just the filenames for a cleaner list
    BASENAMES=()
    for f in "${PY_FILES[@]}"; do
        BASENAMES+=("$(basename "$f")")
    done

    CHOSEN=$(pick_script "${BASENAMES[@]}")

    if [ -z "$CHOSEN" ]; then
        exit 0
    fi

    PYTHON_FILE="$SCRIPT_DIR/$CHOSEN"
fi

# ── Sanity checks ─────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    show_error "python3 is not installed or not in PATH."
    exit 1
fi

# ── Already root? Launch directly ────────────────────────────────────────────
if [ "$EUID" -eq 0 ]; then
    python3 "$PYTHON_FILE"
    exit $?
fi

# ── Prompt for password ───────────────────────────────────────────────────────
PASSWORD=$(get_password)

if [ $? -ne 0 ] || [ -z "$PASSWORD" ]; then
    exit 0
fi

# ── Validate password ─────────────────────────────────────────────────────────
echo "$PASSWORD" | sudo -S -v 2>/dev/null
if [ $? -ne 0 ]; then
    show_error "Incorrect password.\nScript was not launched."
    exit 1
fi

# ── Launch ────────────────────────────────────────────────────────────────────
echo "$PASSWORD" | sudo -S \
    DISPLAY="$DISPLAY" \
    XAUTHORITY="$XAUTHORITY" \
    WAYLAND_DISPLAY="$WAYLAND_DISPLAY" \
    XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
    python3 "$PYTHON_FILE" &

$SHELL
