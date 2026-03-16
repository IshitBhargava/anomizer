#!/usr/bin/env python3

import os
import sys
import struct
import random
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading

HEAD1 = 0xA3
HEAD2 = 0x95
FMT_MSG_ID = 128
FMT_MSG_LEN = 89

DF_FORMAT = {
    'a': ('64s', 64), 'b': ('<b', 1), 'B': ('<B', 1),
    'c': ('<h', 2), 'C': ('<H', 2), 'd': ('<d', 8),
    'e': ('<i', 4), 'E': ('<I', 4), 'f': ('<f', 4),
    'h': ('<h', 2), 'H': ('<H', 2), 'i': ('<i', 4),
    'I': ('<I', 4), 'L': ('<i', 4), 'M': ('<B', 1),
    'n': ('4s', 4), 'N': ('16s', 16), 'Z': ('64s', 64),
    'q': ('<q', 8), 'Q': ('<Q', 8), 'A': ('128s', 128),
}

UNIT_LATITUDE  = 'D'
UNIT_LONGITUDE = 'U'

FALLBACK_LAT_NAMES = {
    'lat', 'hlat', 'dlat', 'oalat', 'dlt', 'olt', 'elat',
    'olat', 'clat', 'trlat', 'wplat', 'rlat', 'tp_lat',
}
FALLBACK_LNG_NAMES = {
    'lng', 'lon', 'hlon', 'hlng', 'dlng', 'oalng', 'dlg', 'olg',
    'elng', 'olng', 'clng', 'trlng', 'wplng', 'rlng', 'tp_lng',
}


def parse_formats(data):
    fmt_defs = {}
    pos = 0
    end = len(data) - 2
    while pos < end:
        if data[pos] == HEAD1 and data[pos + 1] == HEAD2:
            msg_id = data[pos + 2]
            if msg_id == FMT_MSG_ID:
                if pos + FMT_MSG_LEN <= len(data):
                    type_id = data[pos + 3]
                    msg_len = data[pos + 4]
                    name = data[pos+5:pos+9].decode('ascii', errors='replace').rstrip('\x00')
                    fmt_str = data[pos+9:pos+25].decode('ascii', errors='replace').rstrip('\x00')
                    columns = data[pos+25:pos+89].decode('ascii', errors='replace').rstrip('\x00')
                    fmt_defs[type_id] = (name, msg_len, fmt_str, columns.split(','))
                pos += FMT_MSG_LEN
            elif msg_id in fmt_defs:
                pos += fmt_defs[msg_id][1]
            else:
                pos += 1
        else:
            pos += 1
    return fmt_defs


def parse_fmtu(data, fmt_defs):
    fmtu_tid = None
    for tid, (name, _, _, _) in fmt_defs.items():
        if name == 'FMTU':
            fmtu_tid = tid
            break
    if fmtu_tid is None:
        return {}
    fmtu_len = fmt_defs[fmtu_tid][1]
    fmtu_map = {}
    pos = 0
    end = len(data) - 2
    while pos < end:
        if data[pos] == HEAD1 and data[pos + 1] == HEAD2:
            msg_id = data[pos + 2]
            if msg_id == fmtu_tid and pos + fmtu_len <= len(data):
                fmt_type = data[pos + 11]
                unit_ids = data[pos+12:pos+28].decode('ascii', errors='replace').rstrip('\x00')
                fmtu_map[fmt_type] = unit_ids
                pos += fmtu_len
            elif msg_id in fmt_defs:
                pos += fmt_defs[msg_id][1]
            else:
                pos += 1
        else:
            pos += 1
    return fmtu_map


def compute_field_offsets(fmt_str):
    offset = 3
    offsets = []
    for fc in fmt_str:
        if fc not in DF_FORMAT:
            break
        sfmt, sz = DF_FORMAT[fc]
        offsets.append((offset, fc, sfmt, sz))
        offset += sz
    return offsets


def identify_coord_fields(fmt_defs, fmtu_map, verbose=False, log_func=None):
    use_fmtu = len(fmtu_map) > 0
    result = {}

    for type_id, (name, length, fmt_str, columns) in fmt_defs.items():
        if name in ('FMT', 'FMTU', 'MULT', 'UNIT'):
            continue
        field_offsets = compute_field_offsets(fmt_str)
        if len(field_offsets) != len(columns):
            if len(field_offsets) > len(columns):
                field_offsets = field_offsets[:len(columns)]
            else:
                columns = columns[:len(field_offsets)]
        patches = []

        if use_fmtu and type_id in fmtu_map:
            unit_ids = fmtu_map[type_id]
            for i, uid in enumerate(unit_ids):
                if i >= len(columns) or i >= len(field_offsets):
                    break
                coord_type = None
                if uid == UNIT_LATITUDE:
                    coord_type = 'lat'
                elif uid == UNIT_LONGITUDE:
                    coord_type = 'lng'
                if coord_type:
                    boff, fc, sfmt, sz = field_offsets[i]
                    patches.append((columns[i], boff, fc, sfmt, sz, coord_type))
        else:
            for i, col in enumerate(columns):
                if i >= len(field_offsets):
                    break
                cl = col.lower()
                coord_type = None
                if cl in FALLBACK_LAT_NAMES:
                    coord_type = 'lat'
                elif cl in FALLBACK_LNG_NAMES:
                    coord_type = 'lng'
                elif field_offsets[i][1] == 'L':
                    if any(x in cl for x in ['lat', 'lt']):
                        coord_type = 'lat'
                    elif any(x in cl for x in ['lng', 'lon', 'lg']):
                        coord_type = 'lng'
                if coord_type:
                    boff, fc, sfmt, sz = field_offsets[i]
                    patches.append((columns[i], boff, fc, sfmt, sz, coord_type))

        if patches:
            result[type_id] = patches
            if verbose and log_func:
                method = "FMTU" if (use_fmtu and type_id in fmtu_map) else "fallback"
                log_func(f"  {name:8s} (ID={type_id:3d}) [{method}]:")
                for fname, boff, fc, sfmt, sz, ct in patches:
                    log_func(f"    {fname:12s}  offset={boff:3d}  fmt='{fc}'  type={ct}")

    return result


def offset_value(old_val, fc, offset_deg):
    if fc == 'L':
        return int(old_val + offset_deg * 1e7)
    elif fc in ('I',):
        return int(old_val + offset_deg * 1e7) & 0xFFFFFFFF
    elif fc == 'i':
        return int(old_val + offset_deg * 1e7)
    elif fc == 'f':
        if abs(old_val) > 1000:
            return old_val + offset_deg * 1e7
        else:
            return old_val + offset_deg
    elif fc == 'd':
        return old_val + offset_deg
    else:
        return old_val


def anonymize(input_path, output_path, offset_lat, offset_lng,
              verbose=False, dry_run=False, log_func=None):

    def log(msg):
        if log_func:
            log_func(msg)
        else:
            print(msg)

    data = open(input_path, 'rb').read()
    log(f"Loaded {input_path} ({len(data):,} bytes)")

    fmt_defs = parse_formats(data)
    log(f"Found {len(fmt_defs)} message type definitions")

    fmtu_map = parse_fmtu(data, fmt_defs)
    if fmtu_map:
        log(f"Found {len(fmtu_map)} FMTU unit definitions (using unit-based detection)")
    else:
        log("No FMTU messages found — falling back to field-name heuristics")

    log(f"\nCoordinate fields detected:")
    coord_fields = identify_coord_fields(fmt_defs, fmtu_map, verbose=verbose, log_func=log_func)

    if not coord_fields:
        log("  (none found — nothing to anonymize)")
        return False

    log(f"\n  {len(coord_fields)} message types with lat/lon fields:")
    for type_id in sorted(coord_fields.keys()):
        name = fmt_defs[type_id][0]
        fields = ', '.join(f"{p[0]}({p[5]})" for p in coord_fields[type_id])
        log(f"    {name:8s}: {fields}")

    if dry_run:
        log("\n(Dry run — no output written)")
        return True

    log(f"\nAnonymizing: lat offset={offset_lat:+.6f}°, lng offset={offset_lng:+.6f}°")

    out = bytearray(data)
    pos = 0
    end = len(data) - 2
    patched_msgs = 0
    patched_fields = 0

    while pos < end:
        if data[pos] == HEAD1 and data[pos + 1] == HEAD2:
            msg_id = data[pos + 2]
            if msg_id == FMT_MSG_ID:
                pos += FMT_MSG_LEN
                continue
            if msg_id not in fmt_defs:
                pos += 1
                continue
            msg_len = fmt_defs[msg_id][1]
            if msg_id in coord_fields and pos + msg_len <= len(data):
                msg_patched = False
                for fname, boff, fc, sfmt, sz, coord_type in coord_fields[msg_id]:
                    abs_off = pos + boff
                    if abs_off + sz > len(data):
                        continue
                    if 's' in sfmt:
                        continue
                    try:
                        (old_val,) = struct.unpack_from(sfmt, data, abs_off)
                    except struct.error:
                        continue
                    if old_val == 0:
                        continue
                    deg_offset = offset_lat if coord_type == 'lat' else offset_lng
                    new_val = offset_value(old_val, fc, deg_offset)
                    try:
                        struct.pack_into(sfmt, out, abs_off, new_val)
                        patched_fields += 1
                        msg_patched = True
                    except (struct.error, OverflowError) as ex:
                        if verbose:
                            mname = fmt_defs[msg_id][0]
                            log(f"  Warning: {mname}.{fname} @ {pos}: {ex}")
                if msg_patched:
                    patched_msgs += 1
            pos += msg_len
        else:
            pos += 1

    with open(output_path, 'wb') as f:
        f.write(out)

    log(f"\nDone!")
    log(f"  Messages patched: {patched_msgs:,}")
    log(f"  Fields patched:   {patched_fields:,}")
    log(f"  Output: {output_path}")
    return True


# ─────────────────────────────────────────────
#  GUI
# ─────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ArduPilot Log Anonymizer")
        self.resizable(True, True)
        self.minsize(620, 580)
        self.configure(bg="#1e1e2e")

        self._build_styles()
        self._build_ui()

    def _build_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        bg    = "#1e1e2e"
        panel = "#2a2a3e"
        acc   = "#7c6af7"
        fg    = "#cdd6f4"
        entry = "#313244"

        style.configure("TFrame",       background=bg)
        style.configure("Panel.TFrame", background=panel)
        style.configure("TLabel",       background=bg,    foreground=fg, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=panel, foreground=fg, font=("Segoe UI", 10))
        style.configure("Head.TLabel",  background=bg,    foreground=acc,
                        font=("Segoe UI", 11, "bold"))
        style.configure("TEntry",       fieldbackground=entry, foreground=fg,
                        insertcolor=fg, borderwidth=0, relief="flat")
        style.configure("TCheckbutton", background=panel, foreground=fg,
                        font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", panel)])
        style.configure("Accent.TButton", background=acc, foreground="#ffffff",
                        font=("Segoe UI", 10, "bold"), borderwidth=0, padding=8)
        style.map("Accent.TButton",
                  background=[("active", "#6a58e0"), ("disabled", "#444")],
                  foreground=[("disabled", "#888")])
        style.configure("TButton", background=entry, foreground=fg,
                        font=("Segoe UI", 10), borderwidth=0, padding=6)
        style.map("TButton", background=[("active", "#404060")])
        style.configure("TSpinbox", fieldbackground=entry, foreground=fg,
                        arrowcolor=fg, borderwidth=0)

    def _section(self, parent, title):
        ttk.Label(parent, text=title, style="Head.TLabel").pack(anchor="w", pady=(14, 4))
        f = ttk.Frame(parent, style="Panel.TFrame", padding=12)
        f.pack(fill="x")
        return f

    def _file_row(self, parent, label, var, browse_cmd):
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, style="Panel.TLabel", width=8).pack(side="left")
        ttk.Entry(row, textvariable=var, width=48).pack(side="left", padx=(4, 6), fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=browse_cmd).pack(side="left")

    def _build_ui(self):
        root_pad = ttk.Frame(self, padding=18)
        root_pad.pack(fill="both", expand=True)

        # Title
        ttk.Label(root_pad, text="ArduPilot Log Anonymizer",
                  font=("Segoe UI", 15, "bold"),
                  foreground="#7c6af7", background="#1e1e2e").pack(anchor="w")
        ttk.Label(root_pad, text="Offset all lat/lon fields in an ArduPilot .bin log",
                  foreground="#6c7086", background="#1e1e2e").pack(anchor="w", pady=(0, 4))

        # ── Files ──
        f_files = self._section(root_pad, "Files")
        self.in_path  = tk.StringVar()
        self.out_path = tk.StringVar()
        self._file_row(f_files, "Input",  self.in_path,  self._browse_input)
        self._file_row(f_files, "Output", self.out_path, self._browse_output)

        # ── Offsets ──
        f_off = self._section(root_pad, "Coordinate Offsets")
        self.random_offsets = tk.BooleanVar(value=True)
        self.offset_lat     = tk.DoubleVar(value=0.0)
        self.offset_lng     = tk.DoubleVar(value=0.0)
        self.seed_enabled   = tk.BooleanVar(value=False)
        self.seed_val       = tk.IntVar(value=42)

        chk = ttk.Checkbutton(f_off, text="Use random offsets (±0.5° – ±2.0°)",
                              variable=self.random_offsets,
                              command=self._toggle_offsets,
                              style="TCheckbutton")
        chk.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        ttk.Label(f_off, text="Lat offset (°):", style="Panel.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 6))
        self.spin_lat = ttk.Spinbox(f_off, from_=-90, to=90, increment=0.1,
                                    textvariable=self.offset_lat, width=10, format="%.4f")
        self.spin_lat.grid(row=1, column=1, sticky="w", padx=(0, 20))

        ttk.Label(f_off, text="Lng offset (°):", style="Panel.TLabel").grid(
            row=1, column=2, sticky="w", padx=(0, 6))
        self.spin_lng = ttk.Spinbox(f_off, from_=-180, to=180, increment=0.1,
                                    textvariable=self.offset_lng, width=10, format="%.4f")
        self.spin_lng.grid(row=1, column=3, sticky="w")

        # Seed row
        seed_row = ttk.Frame(f_off, style="Panel.TFrame")
        seed_row.grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.chk_seed = ttk.Checkbutton(seed_row, text="Use fixed random seed:",
                                         variable=self.seed_enabled,
                                         command=self._toggle_seed,
                                         style="TCheckbutton")
        self.chk_seed.pack(side="left")
        self.spin_seed = ttk.Spinbox(seed_row, from_=0, to=999999,
                                     textvariable=self.seed_val, width=8)
        self.spin_seed.pack(side="left", padx=(8, 0))

        self._toggle_offsets()
        self._toggle_seed()

        # ── Options ──
        f_opt = self._section(root_pad, "Options")
        self.verbose  = tk.BooleanVar(value=False)
        self.dry_run  = tk.BooleanVar(value=False)

        ttk.Checkbutton(f_opt, text="Verbose output (show all detected fields)",
                        variable=self.verbose, style="TCheckbutton").pack(anchor="w", pady=2)
        ttk.Checkbutton(f_opt, text="Dry run (scan only, don't write output file)",
                        variable=self.dry_run, style="TCheckbutton").pack(anchor="w", pady=2)

        # ── Buttons ──
        btn_row = ttk.Frame(root_pad)
        btn_row.pack(fill="x", pady=(14, 0))
        ttk.Button(btn_row, text="Clear Log", command=self._clear_log).pack(side="right", padx=(6, 0))
        self.run_btn = ttk.Button(btn_row, text="  ▶  Anonymize",
                                  style="Accent.TButton", command=self._run)
        self.run_btn.pack(side="right")

        # ── Log ──
        ttk.Label(root_pad, text="Output Log", style="Head.TLabel").pack(anchor="w", pady=(14, 4))
        self.log_box = scrolledtext.ScrolledText(
            root_pad, height=10, state="disabled",
            bg="#11111b", fg="#cdd6f4", insertbackground="#cdd6f4",
            font=("Consolas", 9), relief="flat", borderwidth=0
        )
        self.log_box.pack(fill="both", expand=True)

    # ── Helpers ──

    def _toggle_offsets(self):
        state = "disabled" if self.random_offsets.get() else "normal"
        self.spin_lat.config(state=state)
        self.spin_lng.config(state=state)

    def _toggle_seed(self):
        state = "normal" if self.seed_enabled.get() else "disabled"
        self.spin_seed.config(state=state)

    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="Select ArduPilot .bin log",
            filetypes=[("ArduPilot log", "*.bin"), ("All files", "*.*")]
        )
        if path:
            self.in_path.set(path)
            if not self.out_path.get():
                base, ext = os.path.splitext(path)
                self.out_path.set(f"{base}_anon{ext}")

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Save anonymized log as",
            defaultextension=".bin",
            filetypes=[("ArduPilot log", "*.bin"), ("All files", "*.*")]
        )
        if path:
            self.out_path.set(path)

    def _log(self, msg):
        self.log_box.config(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _clear_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")

    def _run(self):
        in_path  = self.in_path.get().strip()
        out_path = self.out_path.get().strip()

        if not in_path:
            messagebox.showerror("Missing input", "Please select an input file.")
            return
        if not os.path.isfile(in_path):
            messagebox.showerror("File not found", f"Input file not found:\n{in_path}")
            return
        if not out_path and not self.dry_run.get():
            messagebox.showerror("Missing output", "Please specify an output file.")
            return

        # Resolve offsets
        if self.random_offsets.get():
            if self.seed_enabled.get():
                random.seed(self.seed_val.get())
            offset_lat = random.uniform(0.5, 2.0) * random.choice([-1, 1])
            offset_lng = random.uniform(0.5, 2.0) * random.choice([-1, 1])
        else:
            offset_lat = self.offset_lat.get()
            offset_lng = self.offset_lng.get()

        self._clear_log()
        self._log(f"Starting anonymization...")
        self._log(f"  Input:  {in_path}")
        if not self.dry_run.get():
            self._log(f"  Output: {out_path}")
        self._log(f"  Lat offset: {offset_lat:+.6f}°")
        self._log(f"  Lng offset: {offset_lng:+.6f}°")
        if self.dry_run.get():
            self._log("  Mode: DRY RUN")
        self._log("")

        self.run_btn.config(state="disabled")

        def worker():
            try:
                anonymize(
                    in_path, out_path,
                    offset_lat, offset_lng,
                    verbose=self.verbose.get(),
                    dry_run=self.dry_run.get(),
                    log_func=lambda m: self.after(0, self._log, m)
                )
            except Exception as e:
                self.after(0, self._log, f"\nERROR: {e}")
                self.after(0, messagebox.showerror, "Error", str(e))
            finally:
                self.after(0, self.run_btn.config, {"state": "normal"})

        threading.Thread(target=worker, daemon=True).start()


if __name__ == '__main__':
    # If args provided, fall back to CLI mode
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser(description='Anonymize ArduPilot .bin log')
        parser.add_argument('input')
        parser.add_argument('output', nargs='?', default=None)
        parser.add_argument('--offset-lat', type=float, default=None)
        parser.add_argument('--offset-lon', type=float, default=None)
        parser.add_argument('--seed', type=int, default=None)
        parser.add_argument('--verbose', '-v', action='store_true')
        parser.add_argument('--dry-run', action='store_true')
        args = parser.parse_args()

        if args.output is None:
            base, ext = os.path.splitext(args.input)
            args.output = f"{base}_anon{ext}"
        if args.seed is not None:
            random.seed(args.seed)
        if args.offset_lat is None:
            args.offset_lat = random.uniform(0.5, 2.0) * random.choice([-1, 1])
        if args.offset_lon is None:
            args.offset_lon = random.uniform(0.5, 2.0) * random.choice([-1, 1])

        anonymize(args.input, args.output, args.offset_lat, args.offset_lon,
                  verbose=args.verbose, dry_run=args.dry_run)
    else:
        App().mainloop()