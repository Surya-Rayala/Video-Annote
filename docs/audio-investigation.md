# Investigation: "unusual / distorted" audio in the room2–room4 recordings

**Question.** After import, the audio of the three room recordings (room2, room3, room4; two camera
files each, `*_0_c_merged.mp4` / `*_1_c_merged.mp4`) sounded unusual, distorted or lower in quality,
while recordings from other rooms were fine. Is the cause the recordings, decoding, resampling,
channel handling, synchronization, re-encoding, or playback?

**Answer.** The defects are in the recordings themselves, upstream of video-annote: a low-bitrate
speech-codec stage before the files were written (room2: a 16 kHz recording badly upsampled), a
mains-locked hum/buzz, and low or sparse speech level. The app's old re-encode (MP3 → AAC 192 kb/s) was faithful and did not cause the
difference, but it was an unnecessary second lossy generation. It is gone in 0.2.0: imported audio is
now preserved exactly, and import reports these conditions.

All numbers below were measured with ffmpeg 8.0, Apple's AudioToolbox/AVFoundation decoders and
custom analysis scripts; independent re-measurements confirmed the conclusions.

## 1. What the files are

| | room2–4 (problem) | other rooms (fine) |
|---|---|---|
| Video | H.264 High 1920×1080 25 fps, GOP 250 (10 s) | H.264 Baseline 1280×720 ~30 fps, full range, GOP ~1 s |
| Audio | **MP3** (LAME 3.100, CBR 128 kb/s) in MP4, 48 kHz, "stereo" | AAC-LC 32 kHz mono |
| Channels | left and right **bit-identical** (dual-mono) | mono |
| Per room | both camera files carry the same audio track, byte for byte | (cameras also share one track) |
| Start | 0.39–0.43 s of digital silence encoded into the audio | none |
| Loudness | room2 −33.3, **room3 −52.0**, room4 −40.5 LUFS | −10.2 to −25.5 LUFS (originals) |

The MP3 layer is clean: one continuous encode, no header changes, no joins, CRC or decode errors,
continuous timestamps, an unused (all-zero) side channel and a full bit reservoir. It is not a quality
factor.

## 2. Root cause: the recordings

1. **Low-bitrate speech codec before the MP3 stage** (most likely source of the "distorted",
   processed timbre). Rooms 3/4 show 20 ms framing locked to exactly 960 samples at 48 kHz, a 12.0 kHz
   brick-wall and a level step at exactly 8.0 kHz; Opus in hybrid mode at roughly 12–14 kb/s
   reproduces all three together, although the exact codec is inferred. In simulation such a stage
   changes speech about 4 times more than the good rooms' AAC (waveform SNR about 7 dB). Room2 was a
   16 kHz recording upsampled with a poor filter: 8–10 kHz holds a mirror image of 6–8 kHz (mirror
   correlation r = 0.70–0.93), which adds an inharmonic "sizzle" to sibilants and leaves only 8 kHz
   of real bandwidth.
2. **Mains-locked hum/buzz** (the "noisy/humming" impression, mostly in pauses). Rooms 3/4 carry a
   rigid comb at multiples of ~120.03 Hz (twice the 60 Hz mains) that makes up 78–89 % of the
   A-weighted power of the quiet frames and reaches 5–10 kHz; room2 has a 120/240/360 Hz hum. Its
   amplitude and phase are perfectly steady and it entered before the codec stage, i.e. it was
   picked up in the capture chain. Mains locking alone cannot tell a ground loop from nearby
   mains-powered equipment (transformers, lighting ballasts, LED drivers, manikin compressors). During
   speech the speech-to-background ratio of rooms 3/4 is normal (about 17 dB(A)); the buzz dominates
   because speech is sparse. The good rooms have no such hum.
3. **Low level and sparse speech.** room3 is about 30 dB quieter than typical good rooms (rooms 2 and
   4 are within the range of accepted sessions, so level alone does not explain them); in rooms 3/4
   only 6.5–8.6 % of 200 ms frames contain speech more than 6 dB above the background. Turning the
   volume up raises the buzz and codec artefacts with it.

Ruled out by measurement: AGC/limiter pumping, noise gates, noise-suppression artefacts, clicks,
dropouts, chunk splices, clipping, damage from the MP3 stage, and sample-rate or speed errors (the
buzz sits exactly at 2 × mains frequency, and voice pitch is normal).

## 3. The app was not the cause, but it changed things it did not need to

- **Old re-encode (AAC 192 kb/s):** time-aligned to the sample, SNR ≈ 42 dB against the source in
  every populated band, worst 20 ms frame 31 dB on room3. That is at least as faithful as the same
  re-encode of a good-room file (worst frame 10 dB). It added a second lossy generation, though.
- **Playback (Qt on macOS → AVFoundation):** what AVPlayer renders matches ffmpeg's decode of the
  stored AAC to 136–138 dB, with mute and volume applied reliably. Exact seeks on a playing player do
  drop 85–170 ms of audio; the new player engine never seeks the audible player to correct drift.
- **MP3 inside MP4 is invisible to Apple's media stack.** AVFoundation (used by Qt on macOS,
  QuickTime, Quick Look) exposes no audio track for MP3 in MP4: the original camera files play
  *silently* in QuickTime. So a non-Apple player must have been the "original" reference, and simply
  copying the MP3 into the working copy would give silent playback in the app.

## 4. What changed in 0.2.0

- Audio is **never put through a second lossy encoder**. AAC is copied bit-for-bit; MP3 and other
  codecs are decoded once and stored losslessly as 24-bit ALAC (16-bit for 16-bit PCM sources).
  Identical stereo channels are stored once as exact mono. Measured on room2: Apple's decode of the
  new working copy equals ffmpeg's decode of the original MP3 to one 24-bit LSB (SNR 104.5 dB), with
  zero lag and identical sample count. The old AAC copy was at about 42 dB.
- Import measures each recording once and reports the conditions above: loudness, peak, dual-mono,
  leading digital silence and **mains hum/buzz**. Rooms 2–4 get the warning "steady 120 Hz hum/buzz
  locked to the mains frequency in the recording", with harmonics 12–20 dB above neighbouring bands;
  none of the 35 good-room and Tobii recordings checked is flagged. The facts are stored per video
  in `session.json` and shown under **Session Info**. The check compares narrow bands on the mains
  harmonics with the bands between them over the whole recording, so it is not fooled by speech or
  by monitor-style beeps (100–200 ms tones, tested at 880–2400 Hz); loud, very short tone bursts
  (20–50 ms at −6 dBFS) repeating exactly once a second can make it report a mains hum (50–120 Hz)
  that is not there. It only adds a warning: the audio is never changed.
- Exported MP4 clips carry the working copy's audio unchanged (ALAC; decoded AAC peaks above full
  scale are limited to full scale), sample-aligned with the frames; dual-mono becomes exact mono.
  The WAVs are 16-bit PCM by default (readable by every tool, correctly rounded); `--wav-format s24`
  keeps 24-bit precision and `f32` the decoded audio exactly.

## 5. What would actually improve these rooms (outside the app)

- **Find the source of the buzz:** a ground loop or power interference in the microphone/capture
  chain (fixes: isolation transformer, balanced or isolated mic feed, separate mic power from the
  camera/PoE supply), or nearby mains-powered equipment (move or switch it off and compare).
- **Raise the capture codec bitrate** in the recording system (Opus ≥ 32 kb/s, or AAC/PCM). Codec
  loss cannot be undone afterwards.
- **Raise the capture gain** or move the microphone closer, especially in room3.
- For ML: treat these rooms as their own audio domain, or resample every room to a common 16 kHz mono
  so that band-edge differences disappear. If you clean the buzz, keep the raw audio and store the
  cleaned variant separately: an adaptive comb canceller reduced the buzz by 17–20 dB in tests, while
  a naive one damaged speech. Normalize loudness in the dataset loader, measured on speech after buzz
  removal, rather than changing the archive.

## 6. Side findings

- The leading 0.39–0.43 s of digital silence was encoded into the MP3 by the merge tool; container
  timestamps place the audio content 0.385–0.417 s after the first video frame, so this offset is
  part of the recording's own A/V alignment and is preserved.
- File-name timestamps are not reliable start times: matching the mains-frequency drift in the buzz
  shows room4's audio starts 15.0 s after room3's (r = 0.997; the two clocks differ by about 5 ppm),
  not the 65 s the file names imply.
