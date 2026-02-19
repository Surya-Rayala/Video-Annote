# video-annote

**Minimum Python:** `>=3.10`

**video-annote** is a lightweight **multi-video annotation tool** (PyQt5) for labeling time ranges (“labels”) while reviewing one or more videos.

You can:
- Create/import sessions that contain multiple videos (local files or URLs)
- Choose a **Time Source** (master timeline) and an **Audio Source**
- Play/pause/restart and scrub safely
- Mark **start/end** for labels and save annotations
- Review and edit annotations via a **timeline** and a **table**
- Autosave session state as you work

> Sync is **best-effort**, not mandatory: the tool uses the Time Source as the reference timeline and keeps other videos close to it during playback, but you can still annotate even if videos differ in duration or aren’t perfectly aligned.

---

## Screenshot

![Video-Annote screenshot](screenshot.png)

---

## Requirements

- **Python >= 3.10**
- **PyQt5**
- **ffmpeg + ffprobe** (recommended)

### Why ffmpeg/ffprobe?
`video-annote` uses `ffmpeg/ffprobe` for:
- importing URL-based videos (including `.m3u8`)
- reading duration/FPS reliably

If you only import local files, the app may still work without ffmpeg, but URL import and some metadata features will be limited.

---

## Install ffmpeg + ffprobe

Pick **one** method below.

### Option A (recommended): Conda
If you plan to use Conda for Python, install ffmpeg into the same environment:

```bash
conda install -c conda-forge ffmpeg -y
```

### Option B: macOS (Homebrew)

```bash
brew install ffmpeg
```

### Option C: Ubuntu / Debian

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg
```

### Option D: Windows
- Install ffmpeg (includes ffprobe) and add it to your **PATH**.

After installing, verify:

```bash
ffmpeg -version
ffprobe -version
```

---

## Recommended: Conda environment setup

This is the most reproducible setup (and the easiest way to ensure ffmpeg/ffprobe are available).

### 1) Create and activate an environment

```bash
conda create -n video-annote python=3.10 -y
conda activate video-annote
```

### 2) Install ffmpeg (inside the env)

```bash
conda install -c conda-forge ffmpeg -y
```

> From here on, **run the remaining install/run steps inside this activated conda environment**.

---

## Install & run

Choose one of the following approaches.

### A) Install from PyPI (pip)

```bash
pip install video-annote
python -m video_annote
```

### B) Install from a local clone (pip editable)

```bash
git clone https://github.com/Surya-Rayala/Video-Annote.git
cd video-annote
pip install -e .
python -m video_annote
```

### C) Using uv (great for development)

```bash
git clone https://github.com/Surya-Rayala/Video-Annote.git
cd video-annote
uv sync
uv run python -m video_annote
```

Notes:
- If you use **uv**, you can still use the system/conda-installed ffmpeg as long as it’s on PATH.
- If you created a **conda env**, make sure it’s activated before running uv commands so the env’s ffmpeg is used.

---

## How to use

### 1) Select a Data Root
Click **Select Root** and choose a folder where sessions will be stored.

### 2) Create or import a session
- **Create New Session**
  - Enter a session label (example: `session_001`)
  - Add videos:
    - **Local file…**
    - **URL…** (downloadable URL or `.m3u8` — requires ffmpeg)
- **Import Existing Session**
  - Loads an already-saved session from the Data Root

### 3) Choose which videos are visible
Use **Selected videos** (multi-select) to choose which videos appear in the grid.

### 4) Pick Time Source and Audio Source
- **Time Source** = master timeline used for the slider and annotation timestamps
- **Audio Source** = which video provides sound

> Other videos are kept aligned to the Time Source during playback when possible.
> If a video is shorter than the current Time Source position, its cell may show black.

### 5) Playback and scrubbing
- **Play / Pause / Restart**
- Drag the timeline slider to seek
- Play is disabled at the end of the Time Source (use Restart)

---

## Creating annotations (label workflow)

### 1) Create Labels
On the right panel (**Labels**):
- Add label number + name (example: `1: Label1`, `2: Label2`, …)
- Each label is assigned a stable color

### 2) Start a label
- Select a label
- Click **Start**
- Move the playhead to where the label begins
- Click **Confirm Start**

### 3) End and save
- Play forward (or scrub) to where the label ends
- Click **End**
- Adjust the end position if needed
- Click **Confirm End** to save (confidence + notes)

This saves an **annotation** for that label.

---

## Timeline & table (review + editing)

### Timeline
The timeline shows all labels as colored blocks:
- Click a block to view details
- Edit to adjust start/end (drag handles)

### Table
The table shows every annotation row:
- Derived fields are locked for safety
- Only key fields can be edited (start/end time, label number, confidence, notes)

---

## Running from Python (advanced)

```python
from PyQt5.QtWidgets import QApplication
from video_annote.main_window import MainWindow

app = QApplication([])
w = MainWindow()
w.show()
app.exec_()
```

---

## License

This project is licensed under the **MIT License**. See `LICENSE` for details.

---