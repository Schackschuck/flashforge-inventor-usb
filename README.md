# flashforge-inventor-usb

Print on a **Flashforge Inventor** straight from **OrcaSlicer**, streaming the G-code line by line over the USB cable. Nothing is written to the printer's internal memory.

Why: on my Inventor, files sent over Wi-Fi (and sometimes over USB) arrived corrupted, and prints stopped halfway. Streaming skips the internal storage completely. It also means you can slice in a modern slicer instead of FlashPrint.

Pure Python standard library (`ctypes` + WinUSB). **Windows only.** Tested on one Inventor (firmware V1.9 20191123). Other old Flashforge machines that speak the same `~M601` protocol over USB may work, but I haven't tried any.

## How it works

```
OrcaSlicer --"Flashforge" print host, 127.0.0.1:8899--> ff_ponte.py --USB, one command at a time--> printer
                                                            |
                                                            +-- web panel: http://localhost:8001
```

`ff_ponte.py` pretends to be the printer for Orca's built-in "Flashforge" upload (the legacy TCP protocol: `~M28` + raw bytes + `~M29`, then `~M23` to print). It saves the file and streams it over USB. The web panel shows progress, height, time left, temperatures and the log, with **Pause**, **Resume** and **Cancel** buttons.

## Setup

1. **USB driver.** Install FlashPrint 5 once: it installs the WinUSB driver for the printer (`VID_2B71&PID_0005`). Untested alternative: Zadig. **Close FlashPrint before printing**, because only one program can hold the USB at a time.
2. **Python 3.8+** on Windows. No packages to install.
3. **Orca printer profile.** Make a custom printer: bed 230 × 150, origin in the center (printable area from -115,-75 to 115,75), height 160, two 0.4 mm nozzles. Then:
   - Start G-code:
     ```
     M118 X115.00 Y75.00 Z160.00 T0 T1
     M140 S[bed_temperature_initial_layer_single] T0
     M104 S[nozzle_temperature_initial_layer] T0
     M107
     G90
     G28
     M132 X Y Z A B
     G1 Z50.000 F420
     G161 X Y F3300
     M7 T0
     M6 T0
     M651 S255
     M907 X100 Y100 Z40 A80 B80
     M108 T0
     G92 E0
     ```
   - End G-code: `M104 S0 T0`, `M104 S0 T1`, `M140 S0 T0`, `G162 Z F1800`, `G1 X0 Y0`, `M652`, `G91`, `M18`
   - Tool change G-code: `M108 T[next_extruder]` then `G92 E0`
4. **Post-processing script** (Process settings → Others → Post-processing scripts):
   `"C:\path\to\python.exe" "C:\path\to\inventor_pp.py";`
   It converts Orca's output into the dialect FlashPrint generates for the Inventor: absolute extrusion per extruder, `M6`/`M7` for the temperature waits, speed limits, line numbers. It also checks that nothing falls off the bed. **Turn off Arc fitting**, because the Inventor has no G2/G3.
5. **Print host.** Click the connection (Wi-Fi) icon next to the printer: host type **Flashforge**, address **127.0.0.1**, serial number and access code empty. Press *Test*.
6. **Start the bridge** and leave its window open:
   ```
   py ff_ponte.py
   ```
   Then in Orca: **Print → Upload and print**. Open http://localhost:8001 to watch.

Optional: set the environment variable `FF_WIFI_IP` to the printer's Wi-Fi IP. If the USB ever stops responding, Cancel then tries to switch the heaters off over the network.

## Other ways to use it

- `py ff_stream.py file.g` prints a file without Orca (or drag the `.g` onto `imprimir.bat`). Ctrl+C cancels.
- `py ff_stream.py file.g --ar` is an "air print": the bed sits 30 mm lower, nothing heats and nothing extrudes. Good for testing motion.
- `py ff_usb.py` reads status. `py ff_usb.py "~M105"` sends any command. `py ff_usb.py --monitor` logs SD print progress.
- `py ff_stream.py --teste` runs the self-check.

## Firmware quirks I found (the useful part)

- **Over USB, every command needs a `~` prefix** (`~G1 X10`). A bare `G1` is silently ignored. Commands go out on bulk endpoint `0x01`, replies come back on `0x81`, and every reply ends with `ok`.
- **Take control with `~M601 S0` over USB.** `S1` (the network one) gets no reply over USB. Release with `~M602`. USB and Wi-Fi work at the same time, but only one holds control.
- **Control expires after a few seconds without a new command**, for example during homing or heating. Without control, the printer **silently ignores** commands: there isn't even a `Received` line. An ignored command doesn't run, so it's safe to retake control and resend it.
- **Built-in flow control:** the motion queue holds about 15 commands. When it's full, the printer delays the `ok`. USB handles ~1800 commands/s, far more than a print needs.
- `~G28`, `~M6` and `~M7` reply `ok` immediately. The *next* command replies `CMD ... Received.` and sends its `ok` only when homing or heating finishes (`MoveMode: WAIT_ON_TOOL`).
- ⚠️ **Never send a command while another one is waiting for its `ok`.** The printer's USB locks up until you unplug the cable, although the board keeps working over Wi-Fi. This happened to me twice: once with status polls during a heat wait, once with a 2 s timeout that gave up too early. `ff_usb.comando()` always waits for the `ok` once `Received` has arrived.
- **Streaming over the built-in Wi-Fi is too slow:** 10–19 commands/s, 34–77 ms each. The Wi-Fi is a serial-to-Wi-Fi module (USR-C322) that batches bytes.
- `~M661` (file list on newer Flashforges) only answers `ok` on the Inventor.

## Limitations

- **The PC has to stay on for the whole print**, with the bridge running. It keeps Windows from sleeping while printing.
- The printer's own screen doesn't show progress, and its pause/cancel buttons don't work: the printer just sees commands arriving, not a print.
- During a homing or heating wait, the panel can't refresh temperatures, and Cancel only takes effect once the wait ends. Sending anything at that moment locks the USB (see above). In an emergency, use the power switch.
- Cancel and Pause finish the ~15 moves already queued before they act.

## Status

Tested so far: an air print of an 18,839-command file (smooth, same time as Orca's estimate), a real 25-minute PLA print sent from Orca, and pause/resume/cancel on a test file. Not yet tested: long prints, and pause/cancel during a real print.

Code comments are in Portuguese.

## License

MIT
