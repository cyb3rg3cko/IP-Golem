"""
IP Switcher
-----------
Manage a list of project → IP address profiles and apply them to a
selected network interface.  No external libraries — stdlib only.

Supports Windows and Linux auto-detected at runtime.
Config saved to ip_switcher_config.json alongside this script.
"""

import json
import os
import platform
import re
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "ip_switcher_config.json")

IS_WINDOWS = platform.system() == "Windows"

DEFAULT_CMD_WINDOWS = (
    'netsh interface ip set address name="{iface}" '
    'static {ip} {mask} {gateway}'
)
DEFAULT_CMD_LINUX = (
    'ip addr flush dev {iface} && '
    'ip addr add {ip}/{prefix} dev {iface} && '
    'ip link set {iface} up'
)
DEFAULT_DHCP_WINDOWS = 'netsh interface ip set address name="{iface}" dhcp'
DEFAULT_DHCP_LINUX   = 'dhclient {iface}'

C_BG     = "#0e0e10"
C_PANEL  = "#18181b"
C_INPUT  = "#3a3a3d"
C_PURPLE = "#9147ff"
C_GREEN  = "#2ec4b6"
C_RED    = "#e91916"
C_ORANGE = "#ff9f1c"
C_TEXT   = "#efeff1"
C_MUTED  = "#adadb8"
C_BORDER = "#3a3a3d"


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def detect_interfaces() -> list[str]:
    try:
        if IS_WINDOWS:
            out = subprocess.check_output(
                ["netsh", "interface", "show", "interface"],
                text=True, stderr=subprocess.DEVNULL
            )
            ifaces = []
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[0] in ("Enabled", "Disabled"):
                    ifaces.append(" ".join(parts[3:]))
            return ifaces if ifaces else ["Ethernet"]
        else:
            out = subprocess.check_output(
                ["ip", "-o", "link", "show"],
                text=True, stderr=subprocess.DEVNULL
            )
            ifaces = []
            for line in out.splitlines():
                m = re.match(r"\d+:\s+(\S+):", line)
                if m and m.group(1) != "lo":
                    ifaces.append(m.group(1))
            return ifaces if ifaces else ["eth0"]
    except Exception:
        return ["eth0"] if not IS_WINDOWS else ["Ethernet"]


def get_interface_ip(iface: str) -> tuple[str, str, bool]:
    """
    Return (ip, mask, is_dhcp) for the given interface.
    ip/mask are empty strings if not found.
    is_dhcp is best-effort on Linux (checks dhclient/NetworkManager).
    """
    try:
        if IS_WINDOWS:
            out = subprocess.check_output(
                ["netsh", "interface", "ip", "show", "config", f"name={iface}"],
                text=True, stderr=subprocess.DEVNULL
            )
            ip, mask, is_dhcp = "", "", False
            for line in out.splitlines():
                line = line.strip()
                if "IP Address:" in line or "IP-адрес:" in line:
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
                    if m:
                        ip = m.group(1)
                if "Subnet Prefix:" in line or "Маска подсети" in line:
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
                    if m:
                        mask = m.group(1)
                if "DHCP" in line and ("Yes" in line or "Да" in line or "Enabled" in line):
                    is_dhcp = True
            return ip, mask, is_dhcp
        else:
            out = subprocess.check_output(
                ["ip", "-o", "-4", "addr", "show", iface],
                text=True, stderr=subprocess.DEVNULL
            )
            ip, mask = "", ""
            for line in out.splitlines():
                m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", line)
                if m:
                    ip = m.group(1)
                    prefix = int(m.group(2))
                    # Convert prefix to dotted mask
                    bits = (0xFFFFFFFF >> (32 - prefix)) << (32 - prefix)
                    mask = ".".join([str((bits >> (8 * i)) & 0xFF)
                                     for i in reversed(range(4))])
                    break
            # Detect DHCP on Linux — check if dhclient or NetworkManager manages it
            is_dhcp = False
            try:
                nm = subprocess.check_output(
                    ["nmcli", "-g", "IP4.METHOD", "device", "show", iface],
                    text=True, stderr=subprocess.DEVNULL
                )
                is_dhcp = "auto" in nm.lower()
            except Exception:
                # Fallback: check for dhclient lease file
                lease_paths = [
                    f"/var/lib/dhcp/dhclient.{iface}.leases",
                    f"/var/lib/dhclient/dhclient-{iface}.lease",
                ]
                is_dhcp = any(os.path.exists(p) for p in lease_paths)
            return ip, mask, is_dhcp
    except Exception:
        return "", "", False


def mask_to_prefix(mask: str) -> str:
    try:
        bits = sum(bin(int(b)).count("1") for b in mask.split("."))
        return str(bits)
    except Exception:
        return "24"


def validate_ip(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def build_command(template: str, iface: str, ip: str,
                  mask: str, gateway: str) -> str:
    prefix = mask_to_prefix(mask) if mask else "24"
    cmd = template
    cmd = cmd.replace("{iface}",   iface)
    cmd = cmd.replace("{ip}",      ip)
    cmd = cmd.replace("{mask}",    mask    or "255.255.255.0")
    cmd = cmd.replace("{gateway}", gateway or "")
    cmd = cmd.replace("{prefix}",  prefix)
    cmd = re.sub(r'\s+$', '', cmd)
    return cmd


def build_dhcp_command(template: str, iface: str) -> str:
    return template.replace("{iface}", iface)


def run_command(command: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output or "(no output)"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    default = {
        "profiles":        [],
        "command_windows": DEFAULT_CMD_WINDOWS,
        "command_linux":   DEFAULT_CMD_LINUX,
        "dhcp_windows":    DEFAULT_DHCP_WINDOWS,
        "dhcp_linux":      DEFAULT_DHCP_LINUX,
        "last_iface":      "",
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            default.update(data)
        except Exception:
            pass
    return default


def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as exc:
        messagebox.showerror("Save Error", f"Could not save config:\n{exc}")


# ---------------------------------------------------------------------------
# GUI helpers
# ---------------------------------------------------------------------------

def styled_entry(parent, var, width=28, show=None):
    kw = dict(textvariable=var, width=width, bg=C_INPUT, fg=C_TEXT,
              insertbackground=C_TEXT, relief=tk.FLAT,
              font=("Consolas", 10), highlightthickness=1,
              highlightcolor=C_PURPLE, highlightbackground=C_BORDER)
    if show:
        kw["show"] = show
    return tk.Entry(parent, **kw)


def styled_button(parent, text, cmd, colour=C_PURPLE, width=14):
    return tk.Button(
        parent, text=text, command=cmd,
        bg=colour, fg=C_TEXT, activebackground=colour,
        font=("Helvetica", 10, "bold"), relief=tk.FLAT,
        padx=10, pady=5, cursor="hand2", width=width,
        highlightthickness=0
    )


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("IP Switcher")
        self.geometry("860x680")
        self.minsize(760, 580)
        self.configure(bg=C_BG)
        self.resizable(True, True)

        self._cfg = load_config()
        self._profiles: list[dict] = self._cfg.get("profiles", [])

        self._build_ui()
        self._refresh_profile_list()
        self._refresh_iface_list()

        # Restore last interface then check its IP
        last = self._cfg.get("last_iface", "")
        if last:
            values = self._iface_combo["values"]
            if last in values:
                self._iface_var.set(last)

        # Bind tab switch to refresh IP status on tab 0
        self._nb.bind("<<NotebookTabChanged>>", self._on_tab_change)

        # Initial IP check after UI is ready
        self.after(200, self._check_current_ip)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        title_frame = tk.Frame(self, bg=C_PANEL, pady=10)
        title_frame.pack(fill=tk.X)
        tk.Label(title_frame, text="⚡ IP Switcher", bg=C_PANEL, fg=C_PURPLE,
                 font=("Helvetica", 16, "bold")).pack(side=tk.LEFT, padx=16)
        os_label = "Windows" if IS_WINDOWS else platform.system()
        tk.Label(title_frame, text=f"Platform: {os_label}", bg=C_PANEL,
                 fg=C_MUTED, font=("Helvetica", 9)).pack(side=tk.RIGHT, padx=16)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook",     background=C_BG,    borderwidth=0)
        style.configure("TNotebook.Tab", background=C_PANEL, foreground=C_TEXT,
                        padding=[14, 6], font=("Helvetica", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", C_PURPLE)],
                  foreground=[("selected", "white")])

        self._nb = ttk.Notebook(self)
        self._nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        switcher_tab = tk.Frame(self._nb, bg=C_BG)
        profiles_tab = tk.Frame(self._nb, bg=C_BG)
        settings_tab = tk.Frame(self._nb, bg=C_BG)
        self._nb.add(switcher_tab, text="🔌 Switch IP")
        self._nb.add(profiles_tab, text="📋 Profiles")
        self._nb.add(settings_tab, text="⚙️  Settings")

        self._build_switcher_tab(switcher_tab)
        self._build_profiles_tab(profiles_tab)
        self._build_settings_tab(settings_tab)

        status_frame = tk.Frame(self, bg=C_PANEL, pady=4)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self._var_status = tk.StringVar(value="Ready.")
        tk.Label(status_frame, textvariable=self._var_status,
                 bg=C_PANEL, fg=C_MUTED, font=("Helvetica", 9),
                 anchor="w").pack(fill=tk.X, padx=12)

    # ── Switch IP tab ─────────────────────────────────────────────────────────

    def _build_switcher_tab(self, parent):

        # ── Current IP banner ─────────────────────────────────────────────────
        banner = tk.Frame(parent, bg=C_PANEL, pady=10, padx=16)
        banner.pack(fill=tk.X, padx=16, pady=(16, 0))

        tk.Label(banner, text="Current Interface Status", bg=C_PANEL,
                 fg=C_PURPLE, font=("Helvetica", 11, "bold")
                 ).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 8))

        tk.Label(banner, text="Interface:", bg=C_PANEL, fg=C_MUTED,
                 font=("Helvetica", 9)).grid(row=1, column=0, sticky="w")
        self._iface_var = tk.StringVar()
        self._iface_combo = ttk.Combobox(banner, textvariable=self._iface_var,
                                          state="readonly", width=22,
                                          font=("Consolas", 10))
        self._iface_combo.grid(row=1, column=1, padx=(4, 12), sticky="w")
        self._iface_combo.bind("<<ComboboxSelected>>", lambda _: self._check_current_ip())

        styled_button(banner, "↺", self._refresh_iface_list,
                      colour=C_INPUT, width=2).grid(row=1, column=2, padx=(0, 20))

        tk.Label(banner, text="Current IP:", bg=C_PANEL, fg=C_MUTED,
                 font=("Helvetica", 9)).grid(row=1, column=3, sticky="w")
        self._var_current_ip = tk.StringVar(value="—")
        tk.Label(banner, textvariable=self._var_current_ip, bg=C_PANEL,
                 fg=C_TEXT, font=("Consolas", 11, "bold"),
                 width=16, anchor="w").grid(row=1, column=4, padx=(4, 12), sticky="w")

        tk.Label(banner, text="Mode:", bg=C_PANEL, fg=C_MUTED,
                 font=("Helvetica", 9)).grid(row=1, column=5, sticky="w")
        self._var_current_mode = tk.StringVar(value="—")
        self._lbl_mode = tk.Label(banner, textvariable=self._var_current_mode,
                                   bg=C_PANEL, fg=C_MUTED,
                                   font=("Helvetica", 10, "bold"), anchor="w")
        self._lbl_mode.grid(row=1, column=6, padx=(4, 20), sticky="w")

        tk.Label(banner, text="Matched profile:", bg=C_PANEL, fg=C_MUTED,
                 font=("Helvetica", 9)).grid(row=1, column=7, sticky="w")
        self._var_matched_profile = tk.StringVar(value="—")
        self._lbl_matched = tk.Label(banner, textvariable=self._var_matched_profile,
                                      bg=C_PANEL, fg=C_MUTED,
                                      font=("Helvetica", 10), anchor="w")
        self._lbl_matched.grid(row=1, column=8, padx=(4, 0), sticky="w")

        # ── Profile selector ──────────────────────────────────────────────────
        profile_frame = tk.Frame(parent, bg=C_PANEL, pady=10, padx=16)
        profile_frame.pack(fill=tk.X, padx=16, pady=(8, 0))

        tk.Label(profile_frame, text="Apply Profile", bg=C_PANEL, fg=C_PURPLE,
                 font=("Helvetica", 11, "bold")).grid(
                 row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        tk.Label(profile_frame, text="Profile:", bg=C_PANEL, fg=C_TEXT,
                 font=("Helvetica", 10)).grid(row=1, column=0, sticky="w")

        self._selected_profile = tk.StringVar()
        self._profile_combo = ttk.Combobox(profile_frame,
                                            textvariable=self._selected_profile,
                                            state="readonly", width=30,
                                            font=("Consolas", 10))
        self._profile_combo.grid(row=1, column=1, padx=(8, 16), sticky="w")
        self._profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)

        # Profile detail preview
        for col, (lbl, attr) in enumerate([
            ("IP:",      "_var_preview_ip"),
            ("Mask:",    "_var_preview_mask"),
            ("Gateway:", "_var_preview_gateway"),
        ]):
            tk.Label(profile_frame, text=lbl, bg=C_PANEL, fg=C_MUTED,
                     font=("Helvetica", 9)).grid(row=1, column=2 + col * 2,
                     sticky="w", padx=(16 if col > 0 else 0, 4))
            var = tk.StringVar(value="—")
            setattr(self, attr, var)
            tk.Label(profile_frame, textvariable=var, bg=C_PANEL, fg=C_TEXT,
                     font=("Consolas", 10)).grid(row=1, column=3 + col * 2, sticky="w")

        # ── Command preview ───────────────────────────────────────────────────
        cmd_frame = tk.Frame(parent, bg=C_PANEL, pady=10, padx=16)
        cmd_frame.pack(fill=tk.X, padx=16, pady=(8, 0))

        tk.Label(cmd_frame, text="Command Preview", bg=C_PANEL, fg=C_PURPLE,
                 font=("Helvetica", 11, "bold")).pack(anchor="w", pady=(0, 6))

        self._var_cmd_preview = tk.StringVar(
            value="Select a profile to preview the command.")
        tk.Label(cmd_frame, textvariable=self._var_cmd_preview,
                 bg=C_INPUT, fg=C_TEXT, font=("Consolas", 9),
                 anchor="w", justify="left", padx=8, pady=6,
                 wraplength=780).pack(fill=tk.X)

        # ── Action buttons ────────────────────────────────────────────────────
        action_frame = tk.Frame(parent, bg=C_BG)
        action_frame.pack(fill=tk.X, padx=16, pady=(14, 0))

        self._btn_apply = styled_button(action_frame, "⚡ Apply IP",
                                        self._apply_ip, colour=C_PURPLE, width=16)
        self._btn_apply.pack(side=tk.LEFT, padx=(0, 10))

        self._btn_dhcp = styled_button(action_frame, "⟳ Switch to DHCP",
                                       self._apply_dhcp, colour=C_GREEN, width=18)
        self._btn_dhcp.pack(side=tk.LEFT, padx=(0, 10))

        styled_button(action_frame, "⟳ Check IP", self._check_current_ip,
                      colour=C_INPUT, width=12).pack(side=tk.LEFT)

        self._var_output = tk.StringVar(value="")
        tk.Label(parent, textvariable=self._var_output,
                 bg=C_BG, fg=C_MUTED, font=("Consolas", 9),
                 anchor="w", justify="left", wraplength=800
                 ).pack(fill=tk.X, padx=16, pady=(8, 0))

    # ── Profiles tab ──────────────────────────────────────────────────────────

    def _build_profiles_tab(self, parent):
        list_frame = tk.Frame(parent, bg=C_BG)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(16, 0))

        tk.Label(list_frame, text="Saved Profiles", bg=C_BG, fg=C_PURPLE,
                 font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 6))

        tree_frame = tk.Frame(list_frame, bg=C_BG)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        style = ttk.Style()
        style.configure("Treeview", background=C_PANEL, foreground=C_TEXT,
                        fieldbackground=C_PANEL, rowheight=28,
                        font=("Consolas", 10))
        style.configure("Treeview.Heading", background=C_INPUT,
                        foreground=C_TEXT, font=("Helvetica", 10, "bold"))
        style.map("Treeview", background=[("selected", C_PURPLE)])

        cols = ("Name", "IP Address", "Subnet Mask", "Gateway")
        self._tree = ttk.Treeview(tree_frame, columns=cols,
                                   show="headings", height=10)
        for col, w in zip(cols, [200, 150, 150, 150]):
            self._tree.heading(col, text=col)
            self._tree.column(col, width=w, anchor="w")

        sb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL,
                            command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        form_frame = tk.Frame(parent, bg=C_PANEL, pady=12, padx=16)
        form_frame.pack(fill=tk.X, padx=16, pady=(8, 8))

        tk.Label(form_frame, text="Add / Edit Profile", bg=C_PANEL, fg=C_PURPLE,
                 font=("Helvetica", 11, "bold")).grid(
                 row=0, column=0, columnspan=8, sticky="w", pady=(0, 8))

        self._var_name    = tk.StringVar()
        self._var_ip      = tk.StringVar(value="192.168.1.")
        self._var_mask    = tk.StringVar(value="255.255.255.0")
        self._var_gateway = tk.StringVar()

        fields = [
            ("Project Name:", "_var_name",    20),
            ("IP Address:",   "_var_ip",      16),
            ("Subnet Mask:",  "_var_mask",    16),
            ("Gateway:",      "_var_gateway", 16),
        ]
        col = 0
        for label, var_name, width in fields:
            tk.Label(form_frame, text=label, bg=C_PANEL, fg=C_TEXT,
                     font=("Helvetica", 10)).grid(row=1, column=col, sticky="w",
                     padx=(0 if col == 0 else 12, 4))
            col += 1
            styled_entry(form_frame, getattr(self, var_name),
                         width=width).grid(row=1, column=col, sticky="w")
            col += 1

        tk.Label(form_frame, text="(Mask and Gateway optional)",
                 bg=C_PANEL, fg=C_MUTED, font=("Helvetica", 8)
                 ).grid(row=2, column=2, columnspan=4, sticky="w", pady=(4, 0))

        btn_frame = tk.Frame(form_frame, bg=C_PANEL)
        btn_frame.grid(row=3, column=0, columnspan=8, sticky="w", pady=(10, 0))

        styled_button(btn_frame, "＋ Add",   self._add_profile,    width=10).pack(side=tk.LEFT, padx=(0, 8))
        styled_button(btn_frame, "✎ Update", self._update_profile, colour=C_GREEN, width=10).pack(side=tk.LEFT, padx=(0, 8))
        styled_button(btn_frame, "✕ Remove", self._remove_profile, colour=C_RED,   width=10).pack(side=tk.LEFT)

    # ── Settings tab ──────────────────────────────────────────────────────────

    def _build_settings_tab(self, parent):
        pad = dict(padx=20, pady=6)

        tk.Label(parent, text="Command Templates", bg=C_BG, fg=C_PURPLE,
                 font=("Helvetica", 12, "bold")).pack(anchor="w", **pad)

        tk.Label(parent,
                 text="Static IP placeholders: {iface} {ip} {mask} {prefix} {gateway}   "
                      "DHCP placeholders: {iface}",
                 bg=C_BG, fg=C_MUTED, font=("Helvetica", 9),
                 wraplength=800, justify="left").pack(anchor="w", padx=20, pady=(0, 8))

        entries = [
            ("Windows — Static IP:",  "command_windows", DEFAULT_CMD_WINDOWS,    "_var_cmd_windows"),
            ("Linux — Static IP:",    "command_linux",   DEFAULT_CMD_LINUX,      "_var_cmd_linux"),
            ("Windows — DHCP:",       "dhcp_windows",    DEFAULT_DHCP_WINDOWS,   "_var_dhcp_windows"),
            ("Linux — DHCP:",         "dhcp_linux",      DEFAULT_DHCP_LINUX,     "_var_dhcp_linux"),
        ]

        for label, key, default, attr in entries:
            tk.Label(parent, text=label, bg=C_BG, fg=C_TEXT,
                     font=("Helvetica", 10)).pack(anchor="w", padx=20, pady=(8, 2))
            var = tk.StringVar(value=self._cfg.get(key, default))
            setattr(self, attr, var)
            row = tk.Frame(parent, bg=C_BG)
            row.pack(fill=tk.X, padx=20)
            tk.Entry(row, textvariable=var, width=82,
                     bg=C_INPUT, fg=C_TEXT, insertbackground=C_TEXT,
                     relief=tk.FLAT, font=("Consolas", 9),
                     highlightthickness=1, highlightcolor=C_PURPLE,
                     highlightbackground=C_BORDER).pack(side=tk.LEFT,
                     ipady=4, fill=tk.X, expand=True)
            tk.Button(row, text="↺",
                      command=lambda k=key, v=var, d=default: (
                          v.set(d), self._save_settings()),
                      bg=C_INPUT, fg=C_TEXT, relief=tk.FLAT,
                      font=("Helvetica", 9), cursor="hand2", padx=6
                      ).pack(side=tk.LEFT, padx=(6, 0), ipady=4)

        tk.Frame(parent, bg=C_BORDER, height=1).pack(fill=tk.X, padx=20, pady=14)

        styled_button(parent, "💾 Save Settings", self._save_settings,
                      colour=C_PURPLE, width=18).pack(anchor="w", padx=20)

        self._var_settings_status = tk.StringVar(value="")
        tk.Label(parent, textvariable=self._var_settings_status,
                 bg=C_BG, fg=C_GREEN, font=("Helvetica", 9)
                 ).pack(anchor="w", padx=20, pady=(6, 0))

    # ── IP detection ──────────────────────────────────────────────────────────

    def _check_current_ip(self, _event=None):
        """Read the current IP of the selected interface and update the banner."""
        iface = self._iface_var.get().strip()
        if not iface:
            return
        self._var_current_ip.set("checking…")
        self._var_current_mode.set("…")
        self._var_matched_profile.set("…")

        def _do_check():
            ip, mask, is_dhcp = get_interface_ip(iface)
            self.after(0, self._update_ip_banner, ip, mask, is_dhcp)

        threading.Thread(target=_do_check, daemon=True).start()

    def _update_ip_banner(self, ip: str, mask: str, is_dhcp: bool):
        if ip:
            self._var_current_ip.set(ip)
        else:
            self._var_current_ip.set("not found")

        if is_dhcp:
            self._var_current_mode.set("DHCP")
            self._lbl_mode.config(fg=C_GREEN)
        else:
            self._var_current_mode.set("Static")
            self._lbl_mode.config(fg=C_ORANGE)

        # Try to match to a saved profile
        matched = next((p for p in self._profiles if p["ip"] == ip), None)
        if matched:
            self._var_matched_profile.set(matched["name"])
            self._lbl_matched.config(fg=C_GREEN)
            # Auto-select it in the profile dropdown
            self._selected_profile.set(matched["name"])
            self._on_profile_selected()
        else:
            self._var_matched_profile.set("no match")
            self._lbl_matched.config(fg=C_MUTED)

        self._set_status(
            f"{self._iface_var.get()}: {ip or 'no IP'} "
            f"({'DHCP' if is_dhcp else 'Static'})"
            + (f" — matched '{matched['name']}'" if matched else "")
        )

    def _on_tab_change(self, _event=None):
        """Re-check IP whenever the Switch IP tab is shown."""
        try:
            if self._nb.index(self._nb.select()) == 0:
                self._check_current_ip()
        except Exception:
            pass

    # ── Actions ───────────────────────────────────────────────────────────────

    def _refresh_iface_list(self):
        ifaces = detect_interfaces()
        self._iface_combo["values"] = ifaces
        if ifaces and not self._iface_var.get():
            self._iface_var.set(ifaces[0])
        self._set_status(f"Found {len(ifaces)} interface(s).")
        self._check_current_ip()

    def _refresh_profile_list(self):
        names = [p["name"] for p in self._profiles]
        self._profile_combo["values"] = names
        self._tree.delete(*self._tree.get_children())
        for p in self._profiles:
            self._tree.insert("", tk.END, values=(
                p.get("name", ""), p.get("ip", ""),
                p.get("mask", ""), p.get("gateway", ""),
            ))

    def _on_profile_selected(self, _event=None):
        name = self._selected_profile.get()
        profile = next((p for p in self._profiles if p["name"] == name), None)
        if not profile:
            return
        self._var_preview_ip.set(profile.get("ip",      "—"))
        self._var_preview_mask.set(profile.get("mask",   "—") or "—")
        self._var_preview_gateway.set(profile.get("gateway", "—") or "—")
        self._update_cmd_preview(profile)

    def _update_cmd_preview(self, profile: dict):
        iface = self._iface_var.get() or "<interface>"
        template = (self._cfg.get("command_windows", DEFAULT_CMD_WINDOWS)
                    if IS_WINDOWS else
                    self._cfg.get("command_linux",   DEFAULT_CMD_LINUX))
        cmd = build_command(template, iface,
                            profile.get("ip",      ""),
                            profile.get("mask",    ""),
                            profile.get("gateway", ""))
        self._var_cmd_preview.set(cmd)

    def _on_tree_select(self, _event=None):
        selected = self._tree.selection()
        if not selected:
            return
        vals = self._tree.item(selected[0], "values")
        if len(vals) >= 4:
            self._var_name.set(vals[0])
            self._var_ip.set(vals[1])
            self._var_mask.set(vals[2])
            self._var_gateway.set(vals[3])

    def _apply_ip(self):
        iface = self._iface_var.get().strip()
        name  = self._selected_profile.get().strip()
        if not iface:
            messagebox.showwarning("No Interface", "Please select a network interface.")
            return
        if not name:
            messagebox.showwarning("No Profile", "Please select a profile.")
            return
        profile = next((p for p in self._profiles if p["name"] == name), None)
        if not profile:
            messagebox.showerror("Error", "Selected profile not found.")
            return

        template = (self._cfg.get("command_windows", DEFAULT_CMD_WINDOWS)
                    if IS_WINDOWS else
                    self._cfg.get("command_linux",   DEFAULT_CMD_LINUX))
        cmd = build_command(template, iface,
                            profile["ip"],
                            profile.get("mask",    ""),
                            profile.get("gateway", ""))

        if not messagebox.askyesno("Confirm Apply",
                f"Apply '{name}' to {iface}?\n\n"
                f"IP : {profile['ip']}\n"
                f"Mask: {profile.get('mask','default') or 'default'}\n"
                f"GW  : {profile.get('gateway','none') or 'none'}\n\n"
                f"Command:\n{cmd}"):
            return

        self._run_cmd_async(cmd, on_success=lambda out: (
            self._var_output.set(f"✅ {out[:100]}"),
            self._set_status(f"Applied '{name}' → {profile['ip']} on {iface}."),
            self._check_current_ip(),
        ), on_fail=lambda out: (
            self._var_output.set(f"❌ {out[:100]}"),
            self._set_status("IP change failed."),
        ))

    def _apply_dhcp(self):
        iface = self._iface_var.get().strip()
        if not iface:
            messagebox.showwarning("No Interface", "Please select a network interface.")
            return

        template = (self._cfg.get("dhcp_windows", DEFAULT_DHCP_WINDOWS)
                    if IS_WINDOWS else
                    self._cfg.get("dhcp_linux",   DEFAULT_DHCP_LINUX))
        cmd = build_dhcp_command(template, iface)

        if not messagebox.askyesno("Switch to DHCP",
                f"Switch {iface} to DHCP?\n\nCommand:\n{cmd}"):
            return

        self._run_cmd_async(cmd, on_success=lambda out: (
            self._var_output.set(f"✅ DHCP enabled: {out[:100]}"),
            self._set_status(f"Switched {iface} to DHCP."),
            self._check_current_ip(),
        ), on_fail=lambda out: (
            self._var_output.set(f"❌ DHCP failed: {out[:100]}"),
            self._set_status("DHCP switch failed."),
        ))

    def _run_cmd_async(self, cmd: str, on_success, on_fail):
        """Run a shell command in a thread, call success/fail on the main thread."""
        self._btn_apply.config(state=tk.DISABLED)
        self._btn_dhcp.config(state=tk.DISABLED)
        self._set_status("Running command…")

        def _worker():
            success, output = run_command(cmd)
            def _done():
                self._btn_apply.config(state=tk.NORMAL)
                self._btn_dhcp.config(state=tk.NORMAL)
                if success:
                    on_success(output)
                else:
                    on_fail(output)
                    messagebox.showerror("Failed",
                        f"Command failed:\n\n{output}\n\n"
                        "On Linux run with sudo. On Windows run as Administrator.")
            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _add_profile(self):
        name    = self._var_name.get().strip()
        ip      = self._var_ip.get().strip()
        mask    = self._var_mask.get().strip()
        gateway = self._var_gateway.get().strip()
        if not name:
            messagebox.showwarning("Missing Info", "Please enter a project name.")
            return
        if not validate_ip(ip):
            messagebox.showwarning("Invalid IP", f"'{ip}' is not a valid IP address.")
            return
        if mask and not validate_ip(mask):
            messagebox.showwarning("Invalid Mask", f"'{mask}' is not a valid subnet mask.")
            return
        if gateway and not validate_ip(gateway):
            messagebox.showwarning("Invalid Gateway", f"'{gateway}' is not a valid gateway.")
            return
        if any(p["name"] == name for p in self._profiles):
            messagebox.showwarning("Duplicate", f"A profile named '{name}' already exists.")
            return
        self._profiles.append({"name": name, "ip": ip, "mask": mask, "gateway": gateway})
        self._save_profiles()
        self._refresh_profile_list()
        self._clear_form()
        self._set_status(f"Profile '{name}' added.")

    def _update_profile(self):
        selected = self._tree.selection()
        if not selected:
            messagebox.showinfo("Nothing Selected", "Select a profile to update.")
            return
        old_name = self._tree.item(selected[0], "values")[0]
        name     = self._var_name.get().strip()
        ip       = self._var_ip.get().strip()
        mask     = self._var_mask.get().strip()
        gateway  = self._var_gateway.get().strip()
        if not name or not validate_ip(ip):
            messagebox.showwarning("Invalid", "Check the name and IP address.")
            return
        for i, p in enumerate(self._profiles):
            if p["name"] == old_name:
                self._profiles[i] = {"name": name, "ip": ip,
                                      "mask": mask, "gateway": gateway}
                break
        self._save_profiles()
        self._refresh_profile_list()
        self._clear_form()
        self._set_status(f"Profile '{name}' updated.")

    def _remove_profile(self):
        selected = self._tree.selection()
        if not selected:
            messagebox.showinfo("Nothing Selected", "Select a profile to remove.")
            return
        name = self._tree.item(selected[0], "values")[0]
        if not messagebox.askyesno("Confirm", f"Remove profile '{name}'?"):
            return
        self._profiles = [p for p in self._profiles if p["name"] != name]
        self._save_profiles()
        self._refresh_profile_list()
        self._clear_form()
        self._set_status(f"Profile '{name}' removed.")

    def _save_profiles(self):
        self._cfg["profiles"] = self._profiles
        save_config(self._cfg)

    def _save_settings(self):
        self._cfg["command_windows"] = self._var_cmd_windows.get().strip()
        self._cfg["command_linux"]   = self._var_cmd_linux.get().strip()
        self._cfg["dhcp_windows"]    = self._var_dhcp_windows.get().strip()
        self._cfg["dhcp_linux"]      = self._var_dhcp_linux.get().strip()
        save_config(self._cfg)
        self._var_settings_status.set("✅ Settings saved.")
        self.after(3000, lambda: self._var_settings_status.set(""))

    def _clear_form(self):
        self._var_name.set("")
        self._var_ip.set("192.168.1.")
        self._var_mask.set("255.255.255.0")
        self._var_gateway.set("")

    def _set_status(self, msg: str):
        self._var_status.set(msg)

    def _on_close(self):
        self._cfg["last_iface"] = self._iface_var.get()
        save_config(self._cfg)
        self.destroy()


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    App().mainloop()
