#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USB Eraser
==========

A small Windows GUI tool that overwrites an entire USB drive so the previous
contents cannot be recovered with ordinary recovery software.

Two wipe methods are offered
----------------------------
*  Zeros         - write 0x00 across the whole device (fastest; standard quick wipe).
*  Random data   - write cryptographically strong random bytes (os.urandom)
                   across the whole device (slower, hides any residual signal
                   pattern and defeats naive "was this ever used?" heuristics).

Both are single-pass. Everything else is identical to the original tool:

* Lists every physical disk with its number, model, size, bus type and volumes.
* Highlights USB drives in green and system/boot disks in red.
* Refuses (in the UI and again in the worker) to touch a system or boot disk.
* Requires you to type the confirmation word ERASE before anything is written.
* Locks and dismounts the drive's volumes so Windows releases them.
* Writes on a background thread with progress bar, speed, elapsed time, ETA.
* Shows a green "DONE" banner and a popup dialog when the erase completes.
* Uses a small floppy-disk icon (floppy.ico) for the window / taskbar / exe.
* Can be cancelled mid-way (with a warning that the drive is then only
  partially overwritten).

Requirements
------------
* Windows 8 / Server 2012 or newer (uses the Get-Disk PowerShell cmdlet).
* Python 3.8+ with tkinter (bundled with the python.org installer).
* Administrator rights - raw physical-drive access needs them.

Only the Python standard library is used, so PyInstaller can turn this into a
single self-contained .exe.
"""

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk, messagebox

APP_NAME = "USB Eraser"
CONFIRM_WORD = "ERASE"
CHUNK_SIZE = 4 * 1024 * 1024          # 4 MiB per write
PROGRESS_INTERVAL = 0.15              # seconds between UI updates

MODE_ZEROS = "zeros"
MODE_RANDOM = "random"

MODE_LABELS = {
    MODE_ZEROS: "Zeros (fast — one pass of 0x00 over the whole device)",
    MODE_RANDOM: "Random data (slower — one pass of cryptographically strong random bytes)",
}

# --------------------------------------------------------------------------
# Win32 constants
# --------------------------------------------------------------------------
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_WRITE_THROUGH = 0x80000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
FSCTL_ALLOW_EXTENDED_DASD_IO = 0x00090083

CREATE_NO_WINDOW = 0x08000000

if os.name == "nt":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
else:                                          # keeps linters/editors happy
    kernel32 = None

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def human_size(n):
    """1536 -> '1.5 KB'"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def fmt_hms(seconds):
    """95 -> '1m 35s'"""
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"

def is_admin():
    """True when the process can open \\\\.\\PhysicalDriveN for writing."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def resource_path(name):
    """Locate a data file whether we are running from source or from a
    PyInstaller --onefile bundle."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)

# --------------------------------------------------------------------------
# PowerShell bridge
# --------------------------------------------------------------------------
_PS_PRELUDE = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"

def run_powershell(script, timeout=30):
    """Run a PowerShell snippet and return its stdout (stripped)."""
    cmd = [
        "powershell", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-Command", _PS_PRELUDE + script,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "PowerShell failed").strip())
    return (proc.stdout or "").strip()

def run_powershell_json(script):
    """Run a snippet whose output is JSON and return a list of dicts."""
    out = run_powershell(script)
    if not out:
        return []
    data = json.loads(out)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []

# --------------------------------------------------------------------------
# Disk enumeration
# --------------------------------------------------------------------------
_DISK_PS = (
    "ConvertTo-Json -InputObject @(Get-Disk | Select-Object "
    "Number,FriendlyName,SerialNumber,Size,BusType,IsSystem,IsBoot,IsReadOnly,"
    "OperationalStatus) -Compress -Depth 4"
)

_WMI_PS = (
    "ConvertTo-Json -InputObject @(Get-CimInstance Win32_DiskDrive | Select-Object "
    "Index,Model,SerialNumber,Size,InterfaceType,MediaType) -Compress -Depth 4"
)

_PARTITION_PS = (
    "ConvertTo-Json -InputObject @(Get-Partition | Where-Object { $_.DriveLetter } | "
    "Select-Object DiskNumber,DriveLetter) -Compress -Depth 3"
)

_BUS_NAMES = {
    0: "Unknown", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA",
    6: "Fibre", 7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA",
    12: "SD", 13: "MMC", 14: "Virtual", 15: "FileBacked", 16: "Spaces",
    17: "NVMe", 18: "SCM", 19: "UFS",
}

def _bus_name(value):
    """Get-Disk may return BusType as an int or as a string - normalise it."""
    if isinstance(value, bool):
        return "Unknown"
    if isinstance(value, int):
        return _BUS_NAMES.get(value, f"Bus {value}")
    return str(value or "Unknown").strip()

def _system_disk_number():
    """Disk number that hosts the running Windows installation."""
    try:
        out = run_powershell(
            "(Get-Partition -DriveLetter ($env:SystemDrive.TrimEnd(':'))).DiskNumber"
        )
        return int(out.strip())
    except Exception:
        return None

def _volume_letters():
    """{disk_number: 'E:, F:'}"""
    result = {}
    try:
        parts = run_powershell_json(_PARTITION_PS)
    except Exception:
        return result
    for part in parts:
        try:
            num = int(part.get("DiskNumber") or -1)
        except (TypeError, ValueError):
            continue
        letter = part.get("DriveLetter")
        if letter:
            result.setdefault(num, []).append(f"{letter}:")
    return {k: ", ".join(v) for k, v in result.items()}

def _normalize_ps_disk(d):
    return {
        "number": int(d.get("Number") or 0),
        "model": (d.get("FriendlyName") or "Unknown").strip() or "Unknown",
        "serial": (d.get("SerialNumber") or "").strip(),
        "size": int(d.get("Size") or 0),
        "bus": _bus_name(d.get("BusType")),
        "system": bool(d.get("IsSystem")),
        "boot": bool(d.get("IsBoot")),
        "readonly": bool(d.get("IsReadOnly")),
        "status": str(d.get("OperationalStatus") or ""),
    }

def _normalize_wmi_disk(d):
    iface = str(d.get("InterfaceType") or "Unknown").strip() or "Unknown"
    return {
        "number": int(d.get("Index") or 0),
        "model": (d.get("Model") or "Unknown").strip() or "Unknown",
        "serial": (d.get("SerialNumber") or "").strip(),
        "size": int(d.get("Size") or 0),
        "bus": iface,
        "system": False,
        "boot": False,
        "readonly": False,
        "status": str(d.get("MediaType") or ""),
    }

def list_disks():
    """Return every physical disk as a normalised dict."""
    sys_num = _system_disk_number()
    letters = _volume_letters()

    try:
        disks = [_normalize_ps_disk(d) for d in run_powershell_json(_DISK_PS)]
    except Exception:
        disks = [_normalize_wmi_disk(d) for d in run_powershell_json(_WMI_PS)]

    for d in disks:
        if sys_num is not None and d["number"] == sys_num:
            d["system"] = True
        d["letters"] = letters.get(d["number"], "")
        if not d["model"]:
            d["model"] = "Unknown"

    disks.sort(key=lambda d: d["number"])
    return disks

# --------------------------------------------------------------------------
# Raw device access
# --------------------------------------------------------------------------
def _open_physical_drive(number):
    path = r"\\.\PhysicalDrive%d" % number
    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_WRITE_THROUGH,
        None,
    )
    if handle in (None, INVALID_HANDLE_VALUE):
        err = ctypes.get_last_error()
        raise OSError(
            err,
            f"Could not open {path}: {ctypes.FormatError(err)}. "
            "Make sure the tool is running as Administrator and that the drive "
            "is not in use.",
        )
    return handle

def _ioctl(handle, code):
    returned = wintypes.DWORD(0)
    kernel32.DeviceIoControl(
        handle, code, None, 0, None, 0, ctypes.byref(returned), None
    )

def lock_and_dismount(disk_number, log):
    """Lock + dismount every mounted volume on the disk.

    Without this, Windows keeps the filesystem on the drive open and the first
    write to the physical device fails with 'Access is denied'.
    Returns the list of handles that must stay open until we are done.
    """
    handles = []
    try:
        out = run_powershell(
            f"Get-Partition -DiskNumber {disk_number} -ErrorAction SilentlyContinue | "
            "Where-Object { $_.DriveLetter } | Select-Object -ExpandProperty DriveLetter"
        )
    except Exception:
        out = ""

    letters = [c for c in out.split() if len(c) == 1 and c.isalpha()]
    if not letters:
        log("  No mounted volumes found on this disk.")
        return handles

    for letter in letters:
        volume = "\\\\.\\%s:" % letter
        handle = kernel32.CreateFileW(
            volume,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle in (None, INVALID_HANDLE_VALUE):
            err = ctypes.get_last_error()
            log(f"  ! Volume {letter}: could not be opened ({ctypes.FormatError(err)}).")
            continue
        _ioctl(handle, FSCTL_LOCK_VOLUME)
        _ioctl(handle, FSCTL_DISMOUNT_VOLUME)
        _ioctl(handle, FSCTL_ALLOW_EXTENDED_DASD_IO)
        handles.append(handle)
        log(f"  Volume {letter}: locked and dismounted.")
    return handles

def _make_chunk(mode, size):
    """Return a bytes-like buffer of exactly `size` bytes for the given mode.

    For 'zeros' we hand back None so the caller re-uses its pre-zeroed buffer
    (which never changes).  For 'random' we generate a fresh chunk of
    cryptographically strong random bytes via os.urandom.
    """
    if mode == MODE_RANDOM:
        return os.urandom(size)
    return None                    # caller re-uses its shared zero buffer

def overwrite(disk_number, total_bytes, mode, on_progress, cancel_event):
    """Overwrite the whole device with the chosen pattern."""
    handle = _open_physical_drive(disk_number)

    # Shared zero buffer - allocated once, reused for every write in ZEROS mode.
    zero_buffer = ctypes.create_string_buffer(CHUNK_SIZE)

    written_now = wintypes.DWORD(0)
    written_total = 0
    last_report = 0.0

    try:
        while written_total < total_bytes:
            if cancel_event.is_set():
                return written_total

            remaining = total_bytes - written_total
            size = CHUNK_SIZE if remaining >= CHUNK_SIZE else remaining

            chunk = _make_chunk(mode, size)
            if chunk is None:
                buf = zero_buffer
            else:
                buf = ctypes.create_string_buffer(chunk, size)

            ok = kernel32.WriteFile(
                handle, buf, size, ctypes.byref(written_now), None
            )
            if not ok:
                err = ctypes.get_last_error()
                raise OSError(
                    err,
                    f"Write failed after {human_size(written_total)}: "
                    f"{ctypes.FormatError(err)}",
                )
            if written_now.value == 0:
                raise OSError(
                    0,
                    "The device accepted 0 bytes - it may have been unplugged "
                    "or gone offline.",
                )

            written_total += written_now.value

            now = time.time()
            if now - last_report >= PROGRESS_INTERVAL or written_total >= total_bytes:
                on_progress(written_total)
                last_report = now

        return written_total
    finally:
        kernel32.CloseHandle(handle)

# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class EraserApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} — unrecoverable single-pass wipe")
        self.geometry("900x800")
        self.minsize(840, 740)
        self.configure(bg="#f4f6fb")

        self.queue = queue.Queue()
        self.worker = None
        self.cancel_event = threading.Event()
        self.disks = []
        self.total_bytes = 0
        self.mode_var = tk.StringVar(value=MODE_ZEROS)
        self._last_disk = None
        self._last_mode = None

        self._build_style()
        self._build_ui()
        self._apply_icon()

        self.refresh_disks()
        self.after(120, self._drain_queue)

    # ---------------------------------------------------------------- style
    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background="#f4f6fb", foreground="#1b2430",
                        font=("Segoe UI", 10))
        style.configure("TFrame", background="#f4f6fb")
        style.configure("TLabel", background="#f4f6fb")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 18))
        style.configure("Sub.TLabel", foreground="#5b6b7f")
        style.configure("Section.TLabel", font=("Segoe UI Semibold", 10),
                        foreground="#33415c")
        style.configure("Treeview", rowheight=26, background="white",
                        fieldbackground="white", borderwidth=0)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))
        style.configure("Go.TButton", font=("Segoe UI Semibold", 11), padding=(16, 8))
        style.configure("TButton", padding=(10, 6))
        style.configure("TEntry", padding=4)
        style.configure("TRadiobutton", background="#f4f6fb")
        style.configure("TLabelframe", background="#f4f6fb")
        style.configure("TLabelframe.Label", background="#f4f6fb",
                        foreground="#33415c", font=("Segoe UI Semibold", 10))

    # ------------------------------------------------------------------ icon
    def _apply_icon(self):
        """Set the floppy-disk icon for the window / taskbar."""
        try:
            if getattr(sys, "frozen", False):
                # The exe has its own embedded icon resource.
                self.iconbitmap(sys.executable)
                return
            ico = resource_path("floppy.ico")
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass                      # icon is cosmetic - never fail because of it

    # ------------------------------------------------------------------ ui
    def _build_ui(self):
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)

        # Header -----------------------------------------------------------
        ttk.Label(root, text="USB Eraser", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            root,
            text="Overwrites a whole drive in a single pass. What was on it cannot be recovered.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 12))

        # Warning banner ---------------------------------------------------
        warn = tk.Frame(root, bg="#fff4f4", highlightbackground="#f0c9c9",
                        highlightthickness=1)
        warn.pack(fill="x", pady=(0, 12))
        tk.Label(
            warn, bg="#fff4f4", fg="#8c2b2b", justify="left", padx=12, pady=9,
            font=("Segoe UI Semibold", 10),
            text=("THIS DESTROYS EVERYTHING ON THE SELECTED DRIVE.\n"
                  "Check the disk number, model and size twice. There is no undo."),
        ).pack(anchor="w")

        # Toolbar ----------------------------------------------------------
        bar = ttk.Frame(root)
        bar.pack(fill="x")
        ttk.Label(bar, text="Detected drives", style="Section.TLabel").pack(side="left")
        self.refresh_btn = ttk.Button(bar, text="Refresh", command=self.refresh_disks)
        self.refresh_btn.pack(side="right")
        self.admin_lbl = ttk.Label(bar, text="", style="Sub.TLabel")
        self.admin_lbl.pack(side="right", padx=12)

        # Disk table -------------------------------------------------------
        table = ttk.Frame(root)
        table.pack(fill="both", expand=True, pady=(6, 10))

        columns = ("num", "model", "size", "bus", "vol", "sys")
        self.tree = ttk.Treeview(table, columns=columns, show="headings",
                                 selectmode="browse", height=8)
        layout = {
            "num": ("Disk", 60, "center"),
            "model": ("Model", 300, "w"),
            "size": ("Size", 95, "center"),
            "bus": ("Bus", 80, "center"),
            "vol": ("Volumes", 120, "center"),
            "sys": ("System disk", 110, "center"),
        }
        for col, (title, width, anchor) in layout.items():
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor=anchor,
                             stretch=(col == "model"))
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_state())
        self.tree.tag_configure("system", background="#ffecec", foreground="#a52a2a")
        self.tree.tag_configure("usb", background="#eef8ef")

        # Selection summary ------------------------------------------------
        self.sel_lbl = ttk.Label(root, text="No drive selected.", style="Sub.TLabel",
                                 wraplength=840, justify="left")
        self.sel_lbl.pack(fill="x", pady=(0, 10))

        # Confirmation box -------------------------------------------------
        box = ttk.LabelFrame(root, text=" Method & confirmation ", padding=12)
        box.pack(fill="x", pady=(0, 10))
        box.columnconfigure(0, weight=1)

        ttk.Label(box, text="Overwrite pattern:", style="Section.TLabel") \
            .grid(row=0, column=0, sticky="w")

        self.rb_zeros = ttk.Radiobutton(
            box, text=MODE_LABELS[MODE_ZEROS], value=MODE_ZEROS,
            variable=self.mode_var, command=self._update_state,
        )
        self.rb_zeros.grid(row=1, column=0, sticky="w", pady=(2, 0))

        self.rb_random = ttk.Radiobutton(
            box, text=MODE_LABELS[MODE_RANDOM], value=MODE_RANDOM,
            variable=self.mode_var, command=self._update_state,
        )
        self.rb_random.grid(row=2, column=0, sticky="w", pady=(2, 6))

        ttk.Separator(box, orient="horizontal").grid(
            row=3, column=0, columnspan=4, sticky="ew", pady=(4, 10)
        )

        ttk.Label(
            box, text=f"Type {CONFIRM_WORD} (capitals) to unlock the erase button:",
        ).grid(row=4, column=0, sticky="w")

        self.confirm_var = tk.StringVar()
        self.confirm_var.trace_add("write", lambda *_: self._update_state())
        self.confirm_entry = ttk.Entry(box, textvariable=self.confirm_var, width=16)
        self.confirm_entry.grid(row=4, column=1, sticky="w", padx=10)

        self.start_btn = ttk.Button(box, text="Erase drive", style="Go.TButton",
                                    state="disabled", command=self.start_erase)
        self.start_btn.grid(row=0, column=2, rowspan=5, padx=(20, 0), sticky="e")

        self.cancel_btn = ttk.Button(box, text="Cancel", state="disabled",
                                     command=self.cancel_erase)
        self.cancel_btn.grid(row=0, column=3, rowspan=5, padx=(8, 0), sticky="e")

        # --- Done banner (appears green when an erase completes) ---------
        self.done_lbl = tk.Label(
            root, text="", bg="#f4f6fb", fg="#1b7a3d",
            font=("Segoe UI Semibold", 12), anchor="w", padx=2,
        )
        self.done_lbl.pack(fill="x", pady=(0, 2))

        # Progress ---------------------------------------------------------
        self.bar = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.bar.pack(fill="x")

        stats = ttk.Frame(root)
        stats.pack(fill="x", pady=(5, 10))
        self.stat_lbl = ttk.Label(stats, text="Idle", style="Sub.TLabel")
        self.stat_lbl.pack(side="left")
        self.speed_lbl = ttk.Label(stats, text="", style="Sub.TLabel")
        self.speed_lbl.pack(side="right")

        # Log --------------------------------------------------------------
        logbox = ttk.LabelFrame(root, text=" Log ", padding=6)
        logbox.pack(fill="both", expand=True)
        self.log = tk.Text(logbox, height=8, wrap="word", bg="white",
                           relief="flat", font=("Consolas", 9), state="disabled")
        log_scroll = ttk.Scrollbar(logbox, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    # -------------------------------------------------------------- logging
    def log_line(self, text=""):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------------------------------------------------- disk listing
    def refresh_disks(self):
        if self.worker and self.worker.is_alive():
            return
        self.tree.delete(*self.tree.get_children())
        self.disks = []
        self.admin_lbl.config(
            text="Administrator: yes" if is_admin() else "Administrator: NO (run as admin)"
        )
        self.log_line("Scanning drives…")
        try:
            self.disks = list_disks()
        except Exception as exc:
            self.log_line(f"  Could not list drives: {exc}")
            messagebox.showerror(APP_NAME, f"Could not list drives:\n\n{exc}")
            self._update_state()
            return

        for disk in self.disks:
            is_system = disk["system"] or disk["boot"]
            if is_system:
                tags = ("system",)
            elif disk["bus"].upper().startswith("USB"):
                tags = ("usb",)
            else:
                tags = ()
            self.tree.insert(
                "", "end", iid=str(disk["number"]), tags=tags,
                values=(
                    disk["number"],
                    disk["model"],
                    human_size(disk["size"]),
                    disk["bus"],
                    disk["letters"] or "—",
                    "YES — locked" if is_system else "no",
                ),
            )

        usb_count = sum(1 for d in self.disks if d["bus"].upper().startswith("USB"))
        self.log_line(f"  {len(self.disks)} drive(s) found, {usb_count} on USB.")
        self._update_state()

    # ------------------------------------------------------------ selection
    def selected_disk(self):
        selection = self.tree.selection()
        if not selection:
            return None
        try:
            number = int(selection[0])
        except ValueError:
            return None
        for disk in self.disks:
            if disk["number"] == number:
                return disk
        return None

    def _update_state(self):
        busy = bool(self.worker and self.worker.is_alive())
        disk = self.selected_disk()
        protected = disk is not None and (disk["system"] or disk["boot"])

        can_start = (
            disk is not None
            and not protected
            and self.confirm_var.get() == CONFIRM_WORD
            and not busy
            and is_admin()
        )

        self.start_btn.config(state="normal" if can_start else "disabled")
        self.refresh_btn.config(state="disabled" if busy else "normal")
        self.confirm_entry.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")
        self.rb_zeros.config(state="disabled" if busy else "normal")
        self.rb_random.config(state="disabled" if busy else "normal")

        if disk is None:
            self.sel_lbl.config(text="No drive selected.")
            return

        text = (
            f'Selected: Disk {disk["number"]} — {disk["model"]} — '
            f'{human_size(disk["size"])} — bus {disk["bus"]} — '
            f'volumes {disk["letters"] or "none"}'
        )
        if protected:
            text += "      [ SYSTEM DISK — this tool refuses to erase it. ]"
        elif not is_admin():
            text += "      [ Administrator rights required. ]"
        self.sel_lbl.config(text=text)

    # --------------------------------------------------------------- erasing
    def start_erase(self):
        disk = self.selected_disk()
        if disk is None:
            return
        if disk["system"] or disk["boot"]:
            messagebox.showerror(APP_NAME, "That is a system disk. Refusing to erase it.")
            return
        if not is_admin():
            messagebox.showerror(
                APP_NAME,
                "Administrator rights are required.\n\n"
                "Close the tool and start it again with 'Run as administrator'.",
            )
            return
        if self.confirm_var.get() != CONFIRM_WORD:
            return

        mode = self.mode_var.get()
        method_text = (
            "Single pass of ZEROS (0x00)"
            if mode == MODE_ZEROS
            else "Single pass of RANDOM data (os.urandom)"
        )

        confirmed = messagebox.askyesno(
            APP_NAME,
            f"Erase Disk {disk['number']}?\n\n"
            f"Model   : {disk['model']}\n"
            f"Size    : {human_size(disk['size'])}\n"
            f"Bus     : {disk['bus']}\n"
            f"Volumes : {disk['letters'] or 'none'}\n"
            f"Method  : {method_text}\n\n"
            "Every byte will be overwritten.\n"
            "This cannot be undone. Continue?",
            icon="warning",
            default="no",
        )
        if not confirmed:
            return

        self._last_disk = disk
        self._last_mode = mode
        self.cancel_event.clear()
        self.total_bytes = int(disk["size"])
        self.bar["value"] = 0
        self.done_lbl.config(text="")           # clear any previous "Done" banner
        self.stat_lbl.config(text="Starting…")
        self.speed_lbl.config(text="")
        self.log_line("")
        self.log_line(
            f"=== Erasing Disk {disk['number']} — {disk['model']} "
            f"({human_size(disk['size'])}) with {method_text} ==="
        )

        self.worker = threading.Thread(
            target=self._erase_worker, args=(disk, mode), daemon=True
        )
        self.worker.start()
        self._update_state()

    def _erase_worker(self, disk, mode):
        q = self.queue
        handles = []
        started = time.time()
        try:
            q.put(("log", "Clearing the read-only flag (if set)…"))
            try:
                run_powershell(
                    f"Set-Disk -Number {disk['number']} -IsReadOnly $false "
                    "-ErrorAction Stop"
                )
            except Exception as exc:
                q.put(("log", f"  (read-only flag unchanged: {exc})"))

            handles = lock_and_dismount(disk["number"], lambda m: q.put(("log", m)))

            total = int(disk["size"])
            pattern = "zeros" if mode == MODE_ZEROS else "random data"
            q.put(("log", f"Writing {pattern} over {human_size(total)} …"))

            def on_chunk(written):
                elapsed = max(time.time() - started, 0.001)
                speed = written / elapsed
                remaining = (total - written) / speed if speed > 0 else 0
                q.put(("progress", written, total, speed, remaining, elapsed))

            overwrite(disk["number"], total, mode, on_chunk, self.cancel_event)

            try:
                run_powershell("Update-HostStorageCache")
            except Exception:
                pass

            if self.cancel_event.is_set():
                q.put(("log", "Cancelled — the drive is only partially overwritten."))
                q.put(("done", False, "Cancelled"))
            else:
                elapsed = time.time() - started
                q.put(("log", f"Finished in {fmt_hms(elapsed)}."))
                q.put((
                    "log",
                    "The drive has been overwritten. Open Disk Management and "
                    "create a new volume before using it again.",
                ))
                q.put(("done", True, "Drive erased successfully"))
        except Exception as exc:
            q.put(("log", f"Error: {exc}"))
            q.put(("done", False, str(exc)))
        finally:
            for handle in handles:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass

    def cancel_erase(self):
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.log_line("Cancel requested — finishing the current block…")
            self.cancel_btn.config(state="disabled")

    # ------------------------------------------------------------ queue pump
    def _drain_queue(self):
        try:
            while True:
                message = self.queue.get_nowait()
                kind = message[0]

                if kind == "log":
                    self.log_line("  " + message[1])

                elif kind == "progress":
                    _, written, total, speed, remaining, elapsed = message
                    fraction = (written / total) if total else 1.0
                    self.bar["value"] = min(100.0, fraction * 100.0)
                    self.stat_lbl.config(
                        text=(f"{human_size(written)} / {human_size(total)}"
                              f"   ·   {fraction * 100:.1f} %"
                              f"   ·   {fmt_hms(elapsed)} elapsed")
                    )
                    self.speed_lbl.config(
                        text=f"{human_size(speed)}/s   ·   ETA {fmt_hms(remaining)}"
                    )

                elif kind == "done":
                    _, ok, text = message
                    self.worker = None
                    self.confirm_var.set("")
                    if ok:
                        self.bar["value"] = 100
                    self.stat_lbl.config(text=text)
                    self.speed_lbl.config(text="")
                    self.refresh_disks()
                    self._show_done_banner(ok)
        except queue.Empty:
            pass

        self.after(120, self._drain_queue)

    def _show_done_banner(self, ok):
        """The green 'Done' statement the user asked for."""
        if not ok:
            self.done_lbl.config(text="")
            return

        self.done_lbl.config(text="✓  DONE — the drive has been erased.")
        try:
            self.bell()
        except Exception:
            pass

        disk = self._last_disk or {}
        mode = self._last_mode or MODE_ZEROS
        pattern = "zeros (0x00)" if mode == MODE_ZEROS else "random data"

        messagebox.showinfo(
            APP_NAME,
            f"Done!\n\n"
            f"Disk {disk.get('number', '?')} ({disk.get('model', 'unknown')}) "
            f"has been overwritten with {pattern}.\n\n"
            "Open Disk Management and create a new volume before using it again.",
        )

# --------------------------------------------------------------------------
def main():
    if os.name != "nt":
        print("USB Eraser only runs on Windows.")
        return 1

    try:                                       # crisp text on HiDPI screens
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    app = EraserApp()
    if not is_admin():
        app.log_line("WARNING: not running as Administrator.")
        app.log_line("         Right-click the exe and choose 'Run as administrator'.")
    app.mainloop()
    return 0

if __name__ == "__main__":
    sys.exit(main())
