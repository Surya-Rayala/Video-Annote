# Changelog

## 0.2.0

A rework of the import pipeline, playback engine and storage layer, driven by a review of the
0.1.x code and by measurements on real recordings. Sessions, `config.json` and `label.tsv` written by
0.1.x open unchanged, and `label.tsv` keeps the same columns. Once 0.2.0 has saved a session, open it
only with 0.2.0 or later: notes are now escaped losslessly (`\\`, `\t`, `\r`, `\n`; 0.1.x escaped only
newlines), so 0.1.x misreads notes that contain tabs or backslashes, and it drops the new metadata.

### Audio
- Imported audio is no longer re-encoded to AAC. AAC is copied bit-for-bit; MP3 and other codecs
  are decoded once and stored losslessly (ALAC; identical stereo channels as exact mono; every channel
  kept one to one, also for layouts ALAC does not know, such as 2.1, quad or 7.1). MP3 cannot be
  copied as-is because Apple's media framework (used by Qt on macOS) plays MP3-in-MP4 silently.
- Matroska/WebM recordings with codec pre-roll (AAC, Opus, MP3; e.g. from OBS or browsers) keep audio
  and video sample-aligned: ffmpeg 8.0 and older otherwise shifted the video by 7-19 ms.
- macOS screen recordings (and other AAC from Apple's recorders) play exactly as recorded: their audio
  marks 21–44 ms of encoder warm-up to be skipped in a way a copy loses, so it is decoded once and
  stored losslessly instead, and working copies state where their audio starts in samples (the
  millisecond default started this audio 0.979 ms early). Measured on the project's screen recordings;
  ffmpeg 4.4 lacks the option and keeps milliseconds.
- Import measures each recording's audio: loudness, peak, leading digital silence, dual-mono, and
  mains hum/buzz. Unusual conditions are reported, stored in `session.json`, and shown under
  **Session Info**. See `docs/audio-investigation.md` for the room2–room4 findings: the "distorted"
  sound comes from the recordings themselves (an upstream low-bitrate speech codec, a mains-locked
  120 Hz hum/buzz, low level).

### Accurate labels
- Start/End times are read from the player at the moment of the click and snapped to the start of
  the frame on screen, so `start_time`/`end_time` and `start_frame`/`end_frame` describe exactly the
  frames you saw. 0.1.x read a slider that updated once per second, so labels could be up to ~1 s
  early, and frame numbers were rounded to the nearest frame instead of the displayed one.
- Reaching the end of the time source no longer jumps the player back to 00:00 (the documented
  "known issue"): playback parks on the last frame, and Confirm End works there.
- Players are aligned exactly whenever playback pauses or seeks, and seeks are exact even before the
  first playback (Qt otherwise rounds them to whole seconds). Paused players are held in the middle
  of a frame: Qt's macOS backend can land a seek to a frame's exact start a few microseconds early and
  show the previous frame, which made the picture and the recorded frame differ by one. When paused,
  every camera shows the frame its exported clip will use, and frame stepping never gets stuck.
- **Cancel Label** abandons a label being recorded. The time and audio source can be changed until
  Confirm Start (they are recorded then), and other labels can be deleted during a recording, as in
  0.1.x.
- Edits from the table or timeline are validated on a copy before they touch a record. Timeline edits
  are tied to the record itself, so deleting another row can no longer move an edit onto the wrong
  annotation, and a dragged edge snaps to the frame that was shown under the handle.
- A camera that played to its end shows frames again when you seek back (Qt's macOS backend detached
  its picture), and a camera is shown black exactly when its clip has no frame for that moment.

### Import
- Imports run in the background with progress and Cancel. A session is built in a hidden staging
  folder and only appears when every video imported and validated; failures leave nothing behind, and
  the session that was open stays open.
- Re-creating an existing session moves the old folder to `.video-annote-trash` instead of deleting it
  (0.1.x deleted the folder, including any other files in it, before importing). Session labels are
  validated, so e.g. `.` can no longer resolve to the data root.
- Video becomes constant-frame-rate H.264 with a 1 s keyframe interval: frame *k* is shown during
  `[k/fps, (k+1)/fps)`, and scrubbing seeks 3–4× faster. Every frame goes to the slot nearest to its
  timestamp: ffmpeg's own constant-rate mode left the good-room cameras (start-up bursts, frames
  stamped late) up to two frames (67 ms) behind their audio and the other cameras for whole
  recordings. Variable-frame-rate sources are converted to their nominal constant rate (a frame is
  repeated or dropped only where the source timing is irregular) without the ~40 ms video lag 0.1.x
  introduced: on a Tobii glasses recording, 0 frames dropped instead of 16, mean timing error −0.02
  ms instead of +42 ms. NTSC rates (29.97, 59.94, …) stay exact. Odd frame sizes, interlaced and
  full-range video are handled.
- When a source states two frame rates that disagree and its frame timestamps follow one of them (a
  camera's start-up burst read through HLS made ffprobe state twice the camera's rate), the
  timestamps' rate is used.
- Working copies are checked against the source (the video must end where the source's video ends;
  frame count for constant-rate sources; audio channels); truncated files (also AVI) and downloads
  that lost data (failed or stalled segments) are rejected, and gaps in a local source's timeline and
  decoding errors in damaged files are reported. Sources whose video starts late (e.g. eye-tracker
  recordings), Matroska duration tags, MPEG-TS duration estimates and silent or digitally silent
  audio tracks are handled.
- ffmpeg is also found next to the running Python (conda/venv) and in Homebrew folders. ffmpeg 5.1+
  is recommended; older releases get a compatibility fallback. ffmpeg errors show a short
  explanation, with details on request; a conversion that stops making progress is stopped instead of
  hanging, and network sources time out.

### New
- **Export Clips…** (and `video-annote export`): per annotation, frame-exact MP4 clips per camera
  with the working copy's audio, sample-aligned, plus a WAV of the audio source covering exactly the
  label's span (16-bit by default, 24-bit or float on request; channels and sample rate can be
  changed; where the audio source has no audio the WAV is cut instead of padded with silence, to
  within half a frame, and the cut is reported) and a `manifest.csv`/`manifest.json`. Clips are cut
  on the time-source frames of the label times, `round(t × fps)`, which are `label.tsv`'s frame
  numbers (also for labels made by 0.1.x); rows whose frame numbers do not match their times (e.g. a
  `label.tsv` copied from another session) are reported by export and validate, and recomputed from
  the times when the session is opened. Every output is verified (frames, durations, channels)
  before it is put in place; an export folder mirrors the latest export and belongs to one session;
  a cancelled export leaves the previous one untouched; a working copy that cannot be read only
  loses its own outputs.
- Command line: `video-annote [gui|probe|import|export|validate]`; `video-annote` is installed as a
  command.
- Frame-accurate navigation: time/frame readout, Space (play/pause), ←/→ (one frame),
  Shift+←/→ (one second).
- **Session Info** shows what was detected about each video at import.
- **Recording times**: import keeps when each recording was made, as precisely as the file states it
  (the container's creation time from macOS screen recordings, cameras and MP4/MOV/Matroska/AVI
  writers; `com.apple.quicktime.creationdate`; other date tags; an HLS stream's program date-time;
  Tobii Pro Glasses 3 `recording.g3`), or as entered by hand (New Session, Session Info's **Set
  start…**, `import --start`): as Unix time, or as a date and time written the way recording software
  and recorders write it (year first such as `20261026152732` or `2026-10-26 15:27:32`, month first,
  day first) in a chosen time zone; the moment it stands for is shown before it is confirmed. ffmpeg drops the file's own time from converted files, so it
  was lost before. It is stored per video as `recorded` (start and end in local time with the UTC
  offset and in Unix time, and where it comes from) in `session.json`, kept in the working copy's
  metadata, shown in Session Info and the readout, printed by `probe`, and written to the export
  manifest as `clip_start_unix`/`clip_end_unix`. A time stated without a time zone is read as the
  importing computer's and marked so. Sessions imported earlier get it when opened, from the
  original recording if it is still in place. File names and file dates are not used (unreliable on
  the project's recordings).
- `session.json` keeps what people read first (source, recording time, length, notes in plain words,
  when it was imported) and only the media facts playing and exporting need; codecs, audio statistics
  and import plans no longer clutter it (they go to the log; `probe` shows them). Sessions saved by
  0.2 previews are cleaned the same way when next saved.

### Ease of use
Measured at laptop sizes on a real session, and by an independent review:
- The window fits laptop screens: long data-root and session paths are shortened in the middle
  (with the project's paths the window could not be narrower than 2,118 px, so the Labels panel and
  the Export/Finish buttons were off a 1,470-pt screen); the default size follows the screen; window
  size and layout, the last data root and session, and export choices are remembered.
- A menu bar with the usual shortcuts (⌘N/⌘O new/open session, ⌘I Session Info, ⌘E export,
  undo/redo, quit) and **Help ▸ Keyboard Shortcuts**. Buttons are named after what they do: **New
  Session…**, **Open Session…** (was Import Existing Session), **Delete Annotation**.
- Labeling from the keyboard: **I** starts the selected label and confirms its start, **O** ends it
  and confirms the end, **Esc** cancels; **Home/End**, **Delete**. Single keys never act while typing
  in a text field. Start is available once a label is selected, and the line under the buttons names
  the label that would be recorded.
- **Undo/Redo** of adding, editing and deleting annotations.
- Reviewing: a click on a timeline block or a double-click on a table row shows the label's first
  frame (0.1.x opened a dialog on every click); hovering a block shows its details; double-click opens
  Edit/Delete; Return saves and Esc cancels a timeline edit; a click on the progress bar goes there
  (not while a label is being recorded).
- Frame-exact corrections: **Shift+I**/**Shift+O** set the selected annotation's start/end to the
  frame on screen (dragging a handle moved 11–14 frames per pixel on a laptop), **[**/**]** show its
  first/last frame, and times can be typed as minutes:seconds.
- Timeline: opaque colors with black or white text by brightness (the step number had less than 3:1
  contrast on 9 of the 19 label colors, and navy blocks were invisible), names where they fit, a time
  ruler, blocks sized to the font; it takes only the height its lanes need, so the videos get the
  rest, and a splitter between player and table can be dragged (the table showed under two rows).
- Pickers: a click no longer gives them the keyboard focus (with Time Source focused, ↓ switched the
  time source and ←/→ no longer stepped frames), the mouse wheel no longer changes them, a click
  anywhere on a Camera Views row toggles that camera, and videos are named after their recordings in
  the pickers and in a caption above each view. Time and audio source are disabled, not refused with
  a message, while a label is being recorded.
- Labels panel: the name field no longer collapses (it was 22 px wide at every window size), long
  names wrap, and the next free number is suggested.
- Table: readable column titles (the `label.tsv` names in their tooltips), the repeated session
  column hidden, Tab moves on instead of cycling through every cell, and a right-click menu with Show
  First/Last Frame and Set Start/End to the Frame on Screen.
- Plain words, no technical clutter: Session Info shows each video's length, when it was recorded and
  only what is worth knowing (very quiet or clipped audio, an electrical buzz, missing or damaged
  video, left-out audio tracks), without codecs, rates or import plans; an import shows a summary only
  when there is something to tell; the table shows step, name, start, end, length, confidence and
  notes, with times as 01:04.809 (right-click the titles for views, frames and sources); the session
  field shows its name; a small Keyboard Shortcuts button replaces the line of key hints.
- Dialogs: Session Info is a scrollable window with copyable text; Open Session lists each session's
  videos, labels and last change, with a filter, and preselects the session opened last; New Session
  takes dragged files, reorders videos and enables Import once it can work; the confidence dialog takes
  a digit (0 = 10), Tab leaves the notes and ⌘/Ctrl+Return saves; an export whose options would remove
  earlier MP4 clips or WAVs from its folder asks first.
- Sharper text on scaled Windows and Linux displays.

### Reliability
- Files that cannot be parsed (`config.json`, `session.json`, rows of `label.tsv`) are backed up and
  reported instead of being silently dropped and overwritten. Notes round-trip losslessly (tabs,
  newlines, backslashes, Unicode line separators).
- Failed saves are reported, and Finish Session/quitting no longer discards unsaved work silently.
- If a session's files or the label list were changed elsewhere since they were read (the same
  session or root open in two windows, or another program), the other version is backed up before it
  is replaced, and you are told; changes are found by content, also on FAT32/HFS+ drives. A session
  folder that was replaced, moved or deleted while open is never written into: the labels are saved
  to `.video-annote-recovered`. A session whose `session.json` cannot be read (e.g. its permissions)
  is not opened, instead of being saved with guessed video details.
  `config.json` is only written when the labels or colors changed, and one that can be neither read
  nor copied is never overwritten (select the root again once it is fixed). JSON files saved with a
  byte-order mark or as UTF-16 (Windows editors) load.
- Rows of `label.tsv` that do not fit the header, unknown columns and unreadable entries of
  `config.json` or `session.json` are reported and kept in a backup copy instead of being dropped or
  repaired silently.
- A video whose frame rate could not be measured (no ffprobe, or an unreadable file) cannot be used to
  record or retime labels, also after the session was saved and reopened (0.1.x assumed 30 fps).
- Editing one edge of a label keeps the other edge's frame, also for labels made by 0.1.x (whose
  times lie between frames), and dragging one edge onto the other gives a one-frame label on that
  edge. A session whose folder was replaced or moved while open cannot be exported from that window.
  A data root given as a relative path (`video-annote gui --root data`) opens its videos.
- Unexpected errors are logged and shown instead of closing the app.
- Labels beyond the 50-color palette get distinct colors (0.1.x reused near-identical greens); the
  first 50 are unchanged.
- Labels can no longer end beyond the last video frame when the audio track runs longer than the video.
- Hidden camera views no longer decode during playback.
- The command line only reads where it should (`validate`), and exit codes distinguish problems found
  or failures (1) from commands that could not start (2), e.g. a mistyped root or session.

### Breaking changes (Python API only)
The command and the data root layout are unchanged (`session.json` gains a `recorded` block per
video), the GUI's controls are where they were (some renamed, see Ease of use), and 0.2.0 reads every
file 0.1.x wrote (for the other direction see the top). Code that imports the package's modules directly must be
updated:
- Modules moved: `domain` → `core.models`, `persistence` → `core.storage`, `timeutils` →
  `core.timecode`, `media_import` → `media.ingest`, `widgets.*` → `ui.widgets.*` (`video_grid` →
  `ui.video_grid`), `dialogs.*` → `ui.dialogs.*`. Only `video_annote.main_window` remains as an alias.
- Renamed or replaced functions:
  - `domain.get_skill_color_hex(step_no, cfg)` → `core.colors.color_for_step(step_no,
    cfg.skill_color_map)`; `encode_camid_from_active_views` → `core.models.encode_camid`;
    `decode_camid_to_list` → `core.models.decode_camid`; `SKILL_COLOR_PALETTE_50` →
    `core.colors.PALETTE` (the same 50 colors).
  - `persistence.save_session_meta(session_dir, label, videos)` →
    `core.storage.save_session_meta(state)`; `build_session_meta`, `load_session_meta` and
    `load_videos_from_session` were removed (use `load_session_state`).
  - `timeutils.seconds_to_time_str` and `recompute_totals` were removed (`ms_to_time_str`,
    `recompute_from_times` and `recompute_from_frames` remain in `core.timecode`).
  - `media_import`: `import_local_video_into_session` / `import_url_video_into_session` →
    `sessions.create_session` (a whole session) or `media.ingest.ingest` (one video);
    `ffprobe_get_duration_fps` → `media.probe.probe`; `find_ffmpeg` / `find_ffprobe` →
    `media.ffmpeg.find_toolchain`; `validate_local_video_path`, `validate_video_url` and
    `is_probably_url` (now `is_url`) → `sessions`; the remaining helpers were removed.
  - `app.choose_root_dir` was removed (`run_app` asks for a root when none is given). The widgets'
    and dialogs' methods changed: they are internal to the GUI.
- `load_session_state()` returns `(state, report)`: problems are listed in `report.warnings` and
  `report.skipped_rows` instead of being dropped. `load_annotations_tsv(session_dir,
  legacy_notes=False)` returns `(records, skipped_rows)`; pass `legacy_notes=True` for the
  `label.tsv` of a session whose `session.json` has `meta_version` below 2 (0.1.x escaped only
  newlines), as `load_session_state()` does. `persist_session_state()` returns the backups it made
  and `save_root_config()` the backup of a version changed elsewhere (or `None`). A `config.json`
  that cannot be parsed and a `label.tsv` that is not UTF-8 raise `StorageError` instead of being
  read as empty.

### Project
- Package reorganized into `core/` (model, storage, time math), `media/` (ffmpeg) and `ui/` (Qt).
- Automated tests with synthetic media that has ground truth (frame numbers encoded in pixels, timed
  beeps), real-backend playback and workflow tests on macOS, and an opt-in suite for real recordings.
- Proper build backend (hatchling), single-sourced version, PyQt5 `>=5.15.10,<5.16`; `tzdata` on
  Windows (time zones by name).

## 0.1.3 and earlier
Initial releases.
