#!/usr/bin/env python3

#
# Copyright (C) 2026 EosBandi
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

import os
import sys
import struct
import random
import argparse

HEAD1 = 0xA3
HEAD2 = 0x95
FMT_MSG_ID = 128
FMT_MSG_LEN = 89

DF_FORMAT = {
    'a': ('64s', 64),   # int16[32]
    'b': ('<b',   1),   # int8
    'B': ('<B',   1),   # uint8
    'c': ('<h',   2),   # int16 * 100
    'C': ('<H',   2),   # uint16 * 100
    'd': ('<d',   8),   # double
    'e': ('<i',   4),   # int32 * 100
    'E': ('<I',   4),   # uint32 * 100
    'f': ('<f',   4),   # float
    'h': ('<h',   2),   # int16
    'H': ('<H',   2),   # uint16
    'i': ('<i',   4),   # int32
    'I': ('<I',   4),   # uint32
    'L': ('<i',   4),   # int32 (lat/lng * 1e7)
    'M': ('<B',   1),   # uint8 (flight mode)
    'n': ('4s',   4),   # char[4]
    'N': ('16s', 16),   # char[16]
    'Z': ('64s', 64),   # char[64]
    'q': ('<q',   8),   # int64
    'Q': ('<Q',   8),   # uint64
    'A': ('128s',128),  # int16[64]
}

UNIT_LATITUDE  = 'D'   # deglatitude
UNIT_LONGITUDE = 'U'   # deglongitude

FALLBACK_LAT_NAMES = {
    'lat', 'hlat', 'dlat', 'oalat', 'dlt', 'olt', 'elat',
    'olat', 'clat', 'trlat', 'wplat', 'rlat', 'tp_lat',
}
FALLBACK_LNG_NAMES = {
    'lng', 'lon', 'hlon', 'hlng', 'dlng', 'oalng', 'dlg', 'olg',
    'elng', 'olng', 'clng', 'trlng', 'wplng', 'rlng', 'tp_lng',
}


def parse_formats(data):
    """First pass: extract all FMT message definitions."""
    fmt_defs = {}   # msg_id -> (name, length, fmt_str, columns_list)
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
    """Second pass: extract FMTU unit assignments for each message type."""
    # Find the FMTU message type ID
    fmtu_tid = None
    for tid, (name, _, _, _) in fmt_defs.items():
        if name == 'FMTU':
            fmtu_tid = tid
            break

    if fmtu_tid is None:
        return {}

    fmtu_len = fmt_defs[fmtu_tid][1]
    fmtu_map = {}   # type_id -> unit_ids_string

    pos = 0
    end = len(data) - 2

    while pos < end:
        if data[pos] == HEAD1 and data[pos + 1] == HEAD2:
            msg_id = data[pos + 2]
            if msg_id == fmtu_tid and pos + fmtu_len <= len(data):
                # FMTU layout: header(3) + Q(8) + B(1) + N(16) + N(16)
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
    """Compute byte offset of each field within a message (after the 3-byte header)."""
    offset = 3  # skip header bytes
    offsets = []
    for fc in fmt_str:
        if fc not in DF_FORMAT:
            break
        sfmt, sz = DF_FORMAT[fc]
        offsets.append((offset, fc, sfmt, sz))
        offset += sz
    return offsets


def identify_coord_fields(fmt_defs, fmtu_map, verbose=False):
    """
    Identify all lat/lon fields across all message types.
    Returns: dict of msg_id -> list of (field_name, byte_offset, struct_fmt, coord_type)
    where coord_type is 'lat' or 'lng'.
    """
    use_fmtu = len(fmtu_map) > 0
    result = {}

    for type_id, (name, length, fmt_str, columns) in fmt_defs.items():
        if name in ('FMT', 'FMTU', 'MULT', 'UNIT'):
            continue

        field_offsets = compute_field_offsets(fmt_str)
        if len(field_offsets) != len(columns):
            # Mismatch — skip (shouldn't happen in valid logs)
            if len(field_offsets) > len(columns):
                field_offsets = field_offsets[:len(columns)]
            else:
                columns = columns[:len(field_offsets)]

        patches = []

        if use_fmtu and type_id in fmtu_map:
            # ── Primary method: FMTU unit metadata ──
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
            # ── Fallback: field name heuristics + format char 'L' ──
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
                    # Format char 'L' is specifically for lat/lng int32*1e7
                    # But need to guess which one — check name for hints
                    if any(x in cl for x in ['lat', 'lt']):
                        coord_type = 'lat'
                    elif any(x in cl for x in ['lng', 'lon', 'lg']):
                        coord_type = 'lng'
                if coord_type:
                    boff, fc, sfmt, sz = field_offsets[i]
                    patches.append((columns[i], boff, fc, sfmt, sz, coord_type))

        if patches:
            result[type_id] = patches
            if verbose:
                method = "FMTU" if (use_fmtu and type_id in fmtu_map) else "fallback"
                print(f"  {name:8s} (ID={type_id:3d}) [{method}]:")
                for fname, boff, fc, sfmt, sz, ct in patches:
                    print(f"    {fname:12s}  offset={boff:3d}  fmt='{fc}'  type={ct}")

    return result


def offset_value(old_val, fc, offset_deg):
    """Apply degree offset to a coordinate value based on its storage format."""
    if fc == 'L':
        # int32 lat/lng * 1e7 (the standard ArduPilot format)
        return int(old_val + offset_deg * 1e7)
    elif fc in ('I',):
        # uint32 — used by replay messages (RSO2, RFRN, RSLL)
        # These store lat/lon as uint32 * 1e7 (wrapping around for negative)
        return int(old_val + offset_deg * 1e7) & 0xFFFFFFFF
    elif fc == 'i':
        # int32 — used by ADSB, RGPJ
        return int(old_val + offset_deg * 1e7)
    elif fc == 'f':
        # float — could be degrees or *1e7
        if abs(old_val) > 1000:
            return old_val + offset_deg * 1e7
        else:
            return old_val + offset_deg
    elif fc == 'd':
        # double degrees
        return old_val + offset_deg
    else:
        return old_val


def anonymize(input_path, output_path, offset_lat, offset_lng, verbose=False, dry_run=False):
    """Main anonymization routine."""

    data = open(input_path, 'rb').read()
    print(f"Loaded {input_path} ({len(data):,} bytes)")

    # Pass 1: Parse FMT definitions
    fmt_defs = parse_formats(data)
    print(f"Found {len(fmt_defs)} message type definitions")

    # Pass 2: Parse FMTU unit metadata
    fmtu_map = parse_fmtu(data, fmt_defs)
    if fmtu_map:
        print(f"Found {len(fmtu_map)} FMTU unit definitions (using unit-based detection)")
    else:
        print("No FMTU messages found — falling back to field-name heuristics")

    # Identify coordinate fields
    print(f"\nCoordinate fields detected:")
    coord_fields = identify_coord_fields(fmt_defs, fmtu_map, verbose=verbose)

    if not coord_fields:
        print("  (none found — nothing to anonymize)")
        return

    # Summary
    print(f"\n  {len(coord_fields)} message types with lat/lon fields:")
    for type_id in sorted(coord_fields.keys()):
        name = fmt_defs[type_id][0]
        fields = ', '.join(f"{p[0]}({p[5]})" for p in coord_fields[type_id])
        print(f"    {name:8s}: {fields}")

    if dry_run:
        print("\n(Dry run — no output written)")
        return

    # Pass 3: Patch and write
    print(f"\nAnonymizing: lat offset={offset_lat:+.6f}°, lng offset={offset_lng:+.6f}°")

    out = bytearray(data)   # mutable copy
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

                    # String format chars are not patchable
                    if 's' in sfmt:
                        continue

                    try:
                        (old_val,) = struct.unpack_from(sfmt, data, abs_off)
                    except struct.error:
                        continue

                    # Skip zero / uninitialized values
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
                            print(f"  Warning: {mname}.{fname} @ {pos}: {ex}")

                if msg_patched:
                    patched_msgs += 1

            pos += msg_len
        else:
            pos += 1

    # Write output
    with open(output_path, 'wb') as f:
        f.write(out)

    print(f"\nDone!")
    print(f"  Messages patched: {patched_msgs:,}")
    print(f"  Fields patched:   {patched_fields:,}")
    print(f"  Output: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description='Anonymize ArduPilot .bin log by offsetting all lat/lon fields',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('input', help='Input .bin log file')
    parser.add_argument('output', nargs='?', default=None,
                        help='Output .bin file (default: <input>_anon.bin)')
    parser.add_argument('--offset-lat', type=float, default=None,
                        help='Latitude offset in degrees (default: random ±0.5 to ±2.0)')
    parser.add_argument('--offset-lon', type=float, default=None,
                        help='Longitude offset in degrees (default: random ±0.5 to ±2.0)')
    parser.add_argument('--seed', type=int, default=None,
                        help='Random seed for reproducible offsets')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Show detailed field detection info')
    parser.add_argument('--dry-run', action='store_true',
                        help='Scan and report only, don\'t write output')

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"Error: file not found: {args.input}")
        sys.exit(1)

    if args.output is None:
        base, ext = os.path.splitext(args.input)
        args.output = f"{base}_anon{ext}"

    if args.seed is not None:
        random.seed(args.seed)

    if args.offset_lat is None:
        args.offset_lat = random.uniform(0.5, 2.0) * random.choice([-1, 1])
    if args.offset_lon is None:
        args.offset_lon = random.uniform(0.5, 2.0) * random.choice([-1, 1])

    anonymize(args.input, args.output,
              args.offset_lat, args.offset_lon,
              verbose=args.verbose, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
