> `iota2` - Making Imaginations, Real
>
> <i2.iotasquare@gmail.com>


```
 ██╗ ██████╗ ████████╗ █████╗ ██████╗
 ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
 ██║██║   ██║   ██║   ███████║ █████╔╝
 ██║██║   ██║   ██║   ██╔══██║██╔═══╝
 ██║╚██████╔╝   ██║   ██║  ██║███████╗
 ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
```

# Changelog

Version: <!-- VERSION:START -->v0.6.0<!-- VERSION:END -->

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
- Updated `manual-create-release.yml` to pick release notes from CHANGELOG file.
- Fixed missing versions from CHANGELOG.

## [v0.6.0] - 2025-10-18

- Fixed `manual-create-release.yml` syntax errors.

## [v0.5.0] - 2025-10-18

- Updated `manual-create-release.yml` workflow to enable release between two tags.

## [v0.4.0] - 2025-10-18

- Added `--no-release-notes` option to not release all tags on merge.
- Added `manual-create-release.yml` workflow for creating releases manually.

## [v0.3.0] - 2025-10-18

- Fixed CI/CD Pipeline with release notes.

## [v0.2.0] - 2025-10-18

- Add Unreleased change logs to release notes.

## [v0.1.0] - 2025-10-18

- Added version bump and CHANGELOG.

## [v0.0.1] - 2025-09-10

- CANopen Sniffer (GUI / CLI)
    - Decode PDOs and SDOs with EDS/OD metadata
    - GUI: searchable table, filtering, CSV export, histogram, frame-rate graphs
    - CLI: Rich tables, bus stats, logging, export
    - Modes: Fixed (replace row) or Sequential (append row)
- CANopen Frame Simulator
    - Parses TPDO mappings dynamically from EDS
    - Sends PDOs (auto-generated values) + unmapped OD entries as SDOs
    - Supports heartbeat, timestamp, emergency frames
    - Logging option
- CANopen Node Monitor (CLI with Rich TUI)
    - Uses LocalNode + RemoteNode from EDS
    - Displays raw CAN frames, decoded PDOs, SDO requests/responses
    - Split OD variable tables + live status panel
    - Interactive command input panel
    - CSV export of OD changes + logging

[v0.6.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.5.0...v0.6.0
[v0.5.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.4.0...v0.5.0
[v0.4.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.3.0...v0.4.0
[v0.3.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.2.0...v0.3.0
[v0.2.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.1.0...v0.2.0
[v0.1.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.0.1...v0.1.0
[v0.0.1]: https://github.com/iota2/CANopen-tools-suite/tree/v0.0.1
