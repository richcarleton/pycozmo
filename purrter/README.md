# purrter lab

One process owns Cozmo and serves a dark dashboard at http://localhost:5055.
Branch `purrter-lab` — private playground, not for upstream.

| Bit | From |
|---|---|
| Motion watch → YOLO cat check → ntfy push, reconnect + Wi-Fi rejoin | purrter (le0) |
| PC-mic voice commands (Vosk), PC TTS out of Cozmo's speaker, robot event hooks, head/lift | cozmo-app |
| Browser dashboard + MJPEG feed | CozmoPod |
| Face flash on cat (no moving parts, nothing to swat) | pycozmo 0.9 `display_image` fix |

**Modes:** Doorman (parked, watching, drive/lift locked) and Play (drive pad + WASD, dead-man stop, cliff stop).

## Run
```
cd pycozmo
py -3.14 -m venv .venv
.venv\Scripts\activate
pip install -e .
pip install -r purrter\requirements.txt
python purrter\purrter.py
```
Voice and YOLO are optional; the app runs without them and says so in the log.

Settings are env vars: `PURRTER_TOPIC`, `PURRTER_SSID`, `PURRTER_DOOR_HEAD`, `PURRTER_THRESH`,
`PURRTER_VOLUME`, `PURRTER_VOSK_MODEL`, `PURRTER_HOST`, `PURRTER_PORT`.

## Notes on cozmo-app
- Its "custom firmware install" is a y/n prompt and a print — nothing is flashed.
- It depended on `ai-cozmo` (a renamed pycozmo copy); this uses upstream pycozmo 0.9 instead.
- Left behind: the startup 4-second forward drive (off a shelf, that's a cliff dive), duplicate
  mic readers, the cube template matcher (all three cubes used cube1.png).
