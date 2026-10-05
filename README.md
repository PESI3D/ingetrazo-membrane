# Membrane — tensile skins for IngeTrazo

Spans a **minimal surface** (the shape a soap film takes in a wire frame) inside a loop of edges — for tents, sails, canopies, cushions and form-finding studies.

[Deutsch → LIESMICH.md](LIESMICH.md)

![Membrane](screenshot.webp)

## Installation
1. In IngeTrazo: **Extensions ▸ Open plugins folder** (Windows: `%APPDATA%\ingetrazo\plugins\`, Linux: `~/.local/share/ingetrazo/plugins/`).
2. Copy `membrane_tool.py` into that folder.
3. Restart IngeTrazo → **Extensions ▸ Membrane ▸** *Skin from Edges… · Edit Membrane…* (also on the right-click menu).

Requires IngeTrazo ≥ 0.5 (extension API 2).

## How to use
Select a **closed loop of edges** (loose edges and/or a group of edges) and run **Skin from Edges…**. The loop may be bent in 3D (saddle, hypar, wavy edge).

- **Inner closed loops** — mast rings / skylights (*Holes*) or *Fixed lines* the skin continues past. **Open edge chains** inside are fixed lines (ridge or valley cables); selected **guide points** (Tape Measure) are fixed points (mast tops, low points).
- **Pressure** (−100 … 100 %) inflates the skin outwards or sucks it in; 100 % is a hemisphere over a circle.
- **Tension ratio** (long : short) makes the skin stiffer along one axis (warp : weft).
- **Divisions / Mesh size**, **Iterations** (the status shows *settled* when the shape no longer moves), **Soft edges**, **Flip faces**.
- **Live Preview** builds the skin in the model while the dialog stays open (orbit, pan, zoom); **OK** keeps the current preview without computing again. **Follow selection** picks up a new selection while the dialog is open.
- The result is one group that remembers its source and settings: select it and run **Edit Membrane…** to change anything. One undo step.

## Example file
[`examples/membrane_examples.igz`](examples/membrane_examples.igz) — frames to try, side by side from the origin: hypar, tent with two masts, pressure cushion, ridge cable, wavy edge, point tent (+ guide point).

## Changelog
- **1.1** — own toolbar **Membrane** with one icon per command (Skin from Edges… · Edit Membrane…). It starts on a row of its own under the built-in toolbars; move, float or hide it like those (right-click on a toolbar). Icons drawn in IngeTrazo's own style, they follow the light/dark theme.
- **1.0** — first release.

## Licence
GPL-3.0-or-later · © 2026 Pesi (pesi3d.de) · [Impressum](https://pesi3d.de)
