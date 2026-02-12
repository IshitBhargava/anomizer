# ArduPilot .bin Log Anonymizer

Reads an ArduPilot DataFlash `.bin` log file and anonymizes all GPS/position entries by adding a fixed random offset to every latitude and longitude field. The offset is consistent across the entire file so relative positions and flight paths are preserved, but the absolute location is shifted.

No external dependencies — parses the binary DataFlash format directly.

## Detection Method

Uses FMTU (Format Unit) metadata embedded in every `.bin` log to definitively identify lat/lon fields by their unit type:

| Unit | Description    |
|------|----------------|
| `D`  | deglatitude    |
| `U`  | deglongitude   |

This is 100% reliable regardless of field naming conventions.

Falls back to field-name heuristics + FMT format char `L` for logs that lack FMTU messages (older ArduPilot firmware).

## Usage

```bash
python anonymize_binlog.py <input.bin> [output.bin] [options]
```

## Options

| Option               | Description                              |
|----------------------|------------------------------------------|
| `--offset-lat <deg>` | Latitude offset in degrees (default: random) |
| `--offset-lon <deg>` | Longitude offset in degrees (default: random) |
| `--seed <int>`       | Random seed for reproducible offsets     |
| `--verbose`, `-v`    | Show detailed detection info             |
| `--dry-run`          | Scan only, don't write output            |

## Examples

```bash
python anonymize_binlog.py flight.bin
python anonymize_binlog.py flight.bin anon.bin --seed 42
python anonymize_binlog.py flight.bin --offset-lat 1.5 --offset-lon -0.8
python anonymize_binlog.py flight.bin --dry-run -v
```