#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

TASK_ID = int(os.environ.get("SLURM_PROCID", 0))
TOTAL_TASKS = int(os.environ.get("SLURM_NTASKS", 1))
PROGRESS_INTERVAL = 60  # seconds between progress lines
TMP_SUFFIX = "_temp_conversion"

# State needed by the signal handler
active_ffmpeg_proc = None
active_tmp_output = None
active_local_input = None


def log(msg: str = "") -> None:
  """Print with task prefix and flush immediately (so Slurm logs are live)."""
  stamp = time.strftime("%H:%M:%S")
  print(f"[{stamp}][T{TASK_ID}] {msg}", flush=True)


def fmt_secs(s: float) -> str:
  s = int(max(s, 0))
  return f"{s // 3600:d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def cleanup_active() -> None:
  for p in (active_tmp_output, active_local_input):
    if p is not None:
      Path(p).unlink(missing_ok=True)


def graceful_exit(signum, frame):
  """Handle SIGTERM (scancel) / SIGINT: stop FFmpeg, remove partial files."""
  log(f"Caught signal {signum}. Stopping FFmpeg gracefully...")
  proc = active_ffmpeg_proc
  if proc and proc.poll() is None:
    proc.terminate()
    try:
      proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
      log("FFmpeg didn't exit in time. Forcing kill...")
      proc.kill()
  cleanup_active()
  log("Clean exit. Original file untouched.")
  sys.exit(0)


signal.signal(signal.SIGTERM, graceful_exit)
signal.signal(signal.SIGINT, graceful_exit)


def probe_metadata(file_path: Path) -> str:
  """Step 1: check stream metadata for field order."""
  cmd = [
      "ffprobe", "-v", "error", "-select_streams", "v:0",
      "-show_entries", "stream=field_order", "-of", "json", str(file_path),
  ]
  try:
    res = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=True)
    streams = json.loads(res.stdout).get("streams", [])
    if streams:
      field_order = streams[0].get("field_order", "unknown").lower()
      log(f"  ffprobe: field_order = '{field_order}'")
      return field_order
  except Exception as e:
    log(f"  [Warning] ffprobe failed: {e}")
  return "unknown"


def get_video_duration(file_path: Path) -> float:
  cmd = [
      "ffprobe", "-v", "error", "-show_entries", "format=duration",
      "-of", "default=noprint_wrappers=1:nokey=1", str(file_path),
  ]
  try:
    res = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=True)
    return float(res.stdout.strip())
  except Exception:
    return 0.0


def analyze_pixels_idet(file_path: Path, sample_frames: int = 600) -> bool:
  """Step 2: sample frames with idet. Skips ahead to avoid logos/black."""
  duration = get_video_duration(file_path)
  start = min(600, duration / 2) if duration > 0 else 0
  cmd = [
      "ffmpeg", "-nostats", "-ss", str(int(start)), "-i", str(file_path),
      "-frames:v", str(sample_frames), "-vf", "idet", "-f", "null", "-",
  ]
  try:
    res = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    for line in res.stderr.splitlines():
      if "Multi frame detection" in line:
        tff = int(line.split("TFF:")[1].split()[0])
        bff = int(line.split("BFF:")[1].split()[0])
        prog = int(line.split("Progressive:")[1].split()[0])
        log(f"  idet: TFF={tff} BFF={bff} Progressive={prog}")
        return (tff + bff) > prog
  except Exception as e:
    log(f"  [Warning] idet check failed: {e}")
  return False


def extract_movie_info(file_path: Path):
  year_match = re.search(r"\((\d{4})\)", str(file_path))
  return file_path.stem, (year_match.group(1) if year_match else "")


def run_ffmpeg_with_progress(cmd, total_dur: float) -> int:
  """Run ffmpeg, parse -progress output, print a summary line periodically."""
  global active_ffmpeg_proc
  # stdout: machine-readable progress (parsed here)
  # stderr: inherited -> only warnings/errors land in the .err file
  active_ffmpeg_proc = subprocess.Popen(
      cmd, stdout=subprocess.PIPE, text=True, bufsize=1
  )
  start = time.time()
  last_print = 0.0
  info = {}
  for line in active_ffmpeg_proc.stdout:
    key, _, val = line.strip().partition("=")
    info[key] = val
    if key != "progress":
      continue
    now = time.time()
    if info["progress"] == "continue" and now - last_print < PROGRESS_INTERVAL:
      continue
    last_print = now
    try:
      done = int(info.get("out_time_us", info.get("out_time_ms", "0"))) / 1e6
    except ValueError:
      done = 0.0
    elapsed = now - start
    speed = done / elapsed if elapsed > 0 else 0
    pct = f"{100 * done / total_dur:5.1f}%" if total_dur > 0 else "  ?  "
    eta = fmt_secs((total_dur - done) / speed) if speed > 0 and total_dur else "?"
    log(
        f"  {pct} | video {fmt_secs(done)}/{fmt_secs(total_dur)} | "
        f"fps {info.get('fps', '?')} | speed {info.get('speed', '?')} | "
        f"elapsed {fmt_secs(elapsed)} | ETA {eta}"
    )
  active_ffmpeg_proc.wait()
  rc = active_ffmpeg_proc.returncode
  active_ffmpeg_proc = None
  return rc


def convert_to_av1_locally(file_path: Path, threads: int, crf: int,
                           preset: int) -> bool:
  """Stage to local /tmp, encode to a temp file next to the original,
  verify duration, then atomically replace the original."""
  global active_tmp_output, active_local_input
  title, year = extract_movie_info(file_path)

  # Per-task scratch dir avoids name collisions between tasks on one node
  scratch = Path(f"/tmp/deinterlacer_{os.environ.get('SLURM_JOB_ID', 'x')}_t{TASK_ID}")
  scratch.mkdir(parents=True, exist_ok=True)
  local_input = scratch / file_path.name
  tmp_output = file_path.with_name(file_path.stem + TMP_SUFFIX + ".mkv")
  active_local_input, active_tmp_output = local_input, tmp_output

  log("  Staging input to local scratch...")
  t0 = time.time()
  try:
    shutil.copy2(file_path, local_input)
  except Exception as e:
    log(f"  [Error] Failed to copy to scratch: {e}")
    cleanup_active()
    return False
  log(f"  Staged in {fmt_secs(time.time() - t0)}")

  total_dur = get_video_duration(local_input)

  cmd = [
      "ffmpeg", "-y", "-nostdin", "-nostats", "-loglevel", "warning",
      "-progress", "pipe:1",
      "-i", str(local_input), "-map", "0",
      "-vf", "bwdif=mode=send_field:parity=-1:deint=all",
      "-c:v", "libsvtav1", "-crf", str(crf), "-preset", str(preset),
      "-pix_fmt", "yuv420p10le", "-g", "250",
      "-svtav1-params", f"lp={threads}",
      "-c:a", "copy", "-c:s", "copy",
  ]
  if title:
    cmd += ["-metadata", f"title={title}"]
  if year:
    cmd += ["-metadata", f"date={year}", "-metadata", f"year={year}"]
  cmd.append(str(tmp_output))

  log(f"  Encoding to AV1 50p [Title: {title}, Year: {year}]")
  ok = False
  try:
    rc = run_ffmpeg_with_progress(cmd, total_dur)
    if rc != 0:
      log(f"  [Error] FFmpeg exited with code {rc}. Original kept.")
    else:
      new_dur = get_video_duration(tmp_output)
      if total_dur > 0 and abs(total_dur - new_dur) < 2.0:
        os.replace(tmp_output, file_path)
        size_mb = file_path.stat().st_size / 1e6
        log(f"  Success: verified and replaced original ({size_mb:.0f} MB).")
        ok = True
      else:
        log(
            f"  [CRITICAL] Duration mismatch (in {total_dur:.1f}s vs out "
            f"{new_dur:.1f}s). Original kept, output discarded."
        )
  except Exception as e:
    log(f"  [Error] Conversion failed: {e}")
  finally:
    cleanup_active()
    shutil.rmtree(scratch, ignore_errors=True)
    active_tmp_output = active_local_input = None
  return ok


def find_mkv_files(library_dir: Path):
  files = []
  for root, _dirs, names in os.walk(library_dir, followlinks=True):
    for name in names:
      low = name.lower()
      if low.endswith(".mkv") and not any(
          k in low for k in ("_temp", "_debug", "_av1_test")
      ):
        files.append(Path(root) / name)
  return sorted(files)


def process_library(library_dir: Path, threads: int, crf: int, preset: int):
  try:
    mkv_files = find_mkv_files(library_dir)
  except OSError as e:
    log(f"[Error] Directory traversal failed: {e}")
    return

  my_files = mkv_files[TASK_ID::TOTAL_TASKS]
  log(f"Assigned {len(my_files)} of {len(mkv_files)} total files.")

  converted = skipped = failed = 0
  for i, file_path in enumerate(my_files, 1):
    log("-" * 60)
    log(f"[{i}/{len(my_files)}] {file_path.relative_to(library_dir)}")

    meta = probe_metadata(file_path)
    if meta in ("tt", "tb", "bt", "bb"):
      log("  Metadata flags interlaced content.")
      interlaced = True
    else:
      log("  Metadata ambiguous, running idet pixel check...")
      interlaced = analyze_pixels_idet(file_path)

    if not interlaced:
      log("  Verdict: PROGRESSIVE. Skipping.")
      skipped += 1
      continue

    log("  Verdict: INTERLACED. Converting.")
    if convert_to_av1_locally(file_path, threads, crf, preset):
      converted += 1
    else:
      failed += 1

  log("=" * 60)
  log(f"Done. converted={converted} skipped={skipped} failed={failed}")


def resolve_threads(cli_value) -> int:
  """--threads > SLURM_CPUS_PER_TASK > CPUs available to this process."""
  if cli_value is not None:
    return cli_value
  env = os.environ.get("SLURM_CPUS_PER_TASK")
  if env:
    return int(env)
  return len(os.sched_getaffinity(0))


def main():
  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument("library", type=Path, help="Root directory of the video library")
  ap.add_argument("--threads", type=int, default=None,
                  help="SVT-AV1 logical processors (default: "
                       "SLURM_CPUS_PER_TASK, else all available CPUs)")
  ap.add_argument("--crf", type=int, default=24)
  ap.add_argument("--preset", type=int, default=5)
  args = ap.parse_args()
  if not args.library.is_dir():
    sys.exit(f"Not a directory: {args.library}")
  threads = resolve_threads(args.threads)
  log(f"Using {threads} encoder threads per task.")
  process_library(args.library, threads, args.crf, args.preset)


if __name__ == "__main__":
  main()