# deinterlacer

Finds interlaced `.mkv` files in a video library, deinterlaces them to 50p
(`bwdif`, one frame per field) and re-encodes them to AV1 (SVT-AV1, 10-bit).
Audio and subtitle streams are copied unchanged. Built for a Slurm cluster
(RHEL 9) with the library on an NFS share; files are split across Slurm tasks.

> ## ⚠️ Not for production use
>
> **This is a hobby project and it is destructive by design.** A successful
> conversion **replaces your original file**, and no backup is kept.
>
> - The result is only checked by comparing durations. That does **not**
>   prove the output is intact, so a corrupt or glitchy file can pass.
> - Crashes, node failures, full disks or NFS problems at the wrong moment
>   could leave you with a damaged or missing file.
> - It has only been tested on my own library and setup.
>
> **Keep backups of everything you point it at, and try it on a few copies
> first.** Use at your own risk. See the license for the full disclaimer.

## How it works

1. Check the stream's `field_order` metadata with `ffprobe`.
2. If that is ambiguous, sample frames with FFmpeg's `idet` filter.
3. If interlaced: copy the file to local `/tmp`, encode to a temporary file
   next to the original, compare durations, then replace the original.
4. Progressive files are skipped.

## Requirements

- Python 3.8+ (standard library only)
- `ffmpeg` and `ffprobe` with `libsvtav1` and the `bwdif`/`idet` filters
- Slurm (optional; it also runs standalone)
- Local scratch space in `/tmp` for one movie per task

## Usage

Under Slurm (edit paths and resources in the file first):

```bash
sbatch deinterlacer.slurm
```

Standalone:

```bash
python3 -u deinterlacer.py /path/to/library [--threads N] [--crf 24] [--preset 5]
```

`--threads` defaults to `SLURM_CPUS_PER_TASK`, otherwise all available CPUs.
Logs go to one `.out`/`.err` pair per task in the `logs/` directory.

## License

[PolyForm Noncommercial 1.0.0](LICENSE). In short: you may use, modify and
share this for noncommercial purposes (personal use, hobby projects,
education, research, charities and similar), provided you keep the copyright
notice. Commercial use is not permitted. Not an OSI-approved open source
license.
