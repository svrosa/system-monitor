# System Monitor

Terminal-based Linux system monitor built with Textual.

[Screenshot]

## Features
...

## Requirements
- Linux
- Python >= 3.13
- NVIDIA drivers / nvidia-smi for GPU metrics
- lm-sensors / psutil sensor support for CPU temperature

## Installation

git clone ...
cd system-monitor
uv tool install .

sysmon

## Controls

q       Quit
c       Sort by CPU
g       Sort by GPU
m       Sort by RAM
v       Sort by VRAM
/       Search processes
Esc     Close/clear search
Home    Top
End     Bottom
PgUp    Page up
PgDn    Page down
r       Refresh

## Optional Hyprland integration
...
