#!/bin/bash
#SBATCH --job-name=av1-deinterlacer
#SBATCH --output=/home/simon/pythonS/deinterlacer/logs/deinterlacer_%j.out
#SBATCH --error=/home/simon/pythonS/deinterlacer/logs/deinterlacer_%j.err
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=2
#SBATCH --cpus-per-task=10
#SBATCH --mem=30G
#SBATCH --time=120:00:00
#SBATCH --partition=long
#SBATCH --hint=nomultithread
#SBATCH --signal=B:SIGTERM@30

LOGDIR=/home/simon/pythonS/deinterlacer/logs
LIBRARY=/home/simon/mnt/jellyfin/movies
mkdir -p "$LOGDIR"

echo "=========================================="
echo " SLURM JOB START on $(hostname)"
echo "JobID:        $SLURM_JOB_ID"
echo "Total Tasks:  $SLURM_NTASKS"
echo "CPUs/Task:    $SLURM_CPUS_PER_TASK"
echo "Library:      $LIBRARY"
echo "=========================================="

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export PYTHONUNBUFFERED=1

source /home/simon/venvs/.venv/bin/activate
cd /home/simon/pythonS/deinterlacer

# One log pair per task (%t = task id) so the 4 tasks don't interleave.
# srun -u = unbuffered, python3 -u = unbuffered.
srun -u --cpu-bind=none \
  --output="$LOGDIR/deinterlacer_${SLURM_JOB_ID}_task%t.out" \
  --error="$LOGDIR/deinterlacer_${SLURM_JOB_ID}_task%t.err" \
  python3 -u deinterlacer.py "$LIBRARY" &
SRUN_PID=$!

# --signal=B: only signals the batch shell; forward it to srun
trap 'kill -TERM $SRUN_PID' SIGTERM
wait $SRUN_PID
trap - SIGTERM
wait $SRUN_PID

echo "=== Slurm Job Finished at $(date) ==="