"""
tasks_vm_unified.py — Unified end-to-end Celery pipeline.

Full flow (one task, one researcher submission):
  1.  Provision a new VM from OpenNebula template 14 (Ubuntu 22.04 LTS Fixed)
  2.  Resize disk 0 → 10 240 MB before the VM starts (base image is 97% full)
  3.  Wait until VM reaches RUNNING state (polls every 10 s, timeout 5 min)
  4.  Wait until SSH port 22 is reachable (timeout 2 min)
  5.  Bootstrap: add internet default route + install task-specific libraries
  6.  SCP dataset CSV to the VM
  7.  Generate, upload, and run the analysis script via SSH
  8.  SCP all result files back to app/static/results/<job_id>/
  9.  Update the Job DB record: status, vm_ip, result_path
  10. Terminate the VM (unless keep_vm=True was requested)

Each step writes its name into job.status so the dashboard can show
a live progress timeline while polling /status/<job_id>.

Add to requirements.txt:
    paramiko>=3.4.0
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import traceback
from pathlib import Path

import paramiko
import pyone

from celery_app import celery

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (all overridable via .env)
# ---------------------------------------------------------------------------
ONE_ENDPOINT  = os.getenv("ONE_XMLRPC", "http://localhost:2633/RPC2")
ONE_USER      = os.getenv("ONE_USER",   "oneadmin")
ONE_PASS      = os.getenv("ONE_PASS",   "oneadmin")

TEMPLATE_ID   = 14          # ONLY template 14 boots correctly
DISK_SIZE_MB  = 10_240      # resize from the 2 GB base image
GATEWAY       = "192.168.122.1"
SSH_USER      = "ubuntu"
SSH_KEY_PATH  = os.path.expanduser("~/.ssh/id_rsa")

VM_BOOT_TIMEOUT    = 300
VM_BOOT_INTERVAL   = 10
SSH_READY_TIMEOUT  = 120
SSH_READY_INTERVAL = 5

BASE_DIR    = Path(__file__).parent
RESULTS_DIR = BASE_DIR / "app" / "static" / "results"

# ---------------------------------------------------------------------------
# apt packages installed during bootstrap, keyed by task_type
# ---------------------------------------------------------------------------
BOOTSTRAP_PKGS: dict[str, list[str]] = {
    "python_ds": [
        "python3-pip", "python3-pandas", "python3-numpy",
        "python3-matplotlib", "python3-scipy", "python3-seaborn",
        "python3-sklearn",
    ],
    "octave": ["octave", "octave-statistics", "octave-io", "octave-jsonlab"],
    "r":      ["r-base", "r-cran-tidyverse", "r-cran-ggplot2", "r-cran-caret"],
    "julia":  ["julia"],
}

# ---------------------------------------------------------------------------
# Analysis scripts (generated inline — zero portal-side file dependencies)
# ---------------------------------------------------------------------------
PYTHON_DS_SCRIPT = r'''#!/usr/bin/env python3
"""Auto-generated analysis script — Research Cloud Portal"""
import json, sys, os
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import r2_score, mean_squared_error

csv_path   = sys.argv[1]
output_dir = sys.argv[2]
os.makedirs(output_dir, exist_ok=True)

df = pd.read_csv(csv_path)
results = {
    "rows": int(df.shape[0]), "columns": int(df.shape[1]),
    "column_names": df.columns.tolist(),
    "dtypes": df.dtypes.astype(str).to_dict(),
}

num_cols = df.select_dtypes(include=np.number).columns.tolist()
cat_cols = df.select_dtypes(include="object").columns.tolist()
dupes    = int(df.duplicated().sum())
missing  = int(df.isnull().sum().sum())
df = df.drop_duplicates()
for c in num_cols: df[c] = df[c].fillna(df[c].median())
for c in cat_cols: df[c] = df[c].fillna(df[c].mode()[0] if not df[c].mode().empty else "?")
results["cleaning"] = {"duplicates_removed": dupes, "missing_filled": missing}

if num_cols:
    desc = df[num_cols].describe().round(4)
    results["stats"] = desc.to_dict()
    if len(num_cols) > 1:
        corr  = df[num_cols].corr().round(4)
        pairs = []
        for i, c1 in enumerate(num_cols):
            for c2 in num_cols[i+1:]:
                pairs.append({"col1": c1, "col2": c2, "r": round(float(corr.loc[c1,c2]),4)})
        pairs.sort(key=lambda x: abs(x["r"]), reverse=True)
        results["top_correlations"] = pairs[:5]

charts = []
for col in num_cols[:4]:
    fig, ax = plt.subplots(figsize=(7,4))
    ax.hist(df[col].dropna(), bins=30, color="#00d4ff", edgecolor="#0a0f14", alpha=0.85)
    ax.set_title(f"Distribution of {col}", color="white")
    ax.set_facecolor("#0e1318"); fig.patch.set_facecolor("#080c10")
    ax.tick_params(colors="#94a3b8")
    for sp in ax.spines.values(): sp.set_edgecolor("#1a2230")
    plt.tight_layout()
    fname = f"hist_{col.replace(' ','_').replace('/','_')}.png"
    plt.savefig(os.path.join(output_dir, fname), dpi=100, bbox_inches="tight")
    plt.close()
    charts.append({"type": "histogram", "column": col, "file": fname})

if len(num_cols) > 1:
    corr_data = df[num_cols].corr()
    fig, ax = plt.subplots(figsize=(8,6))
    im = ax.imshow(corr_data.values, cmap="coolwarm", vmin=-1, vmax=1)
    ax.set_xticks(range(len(num_cols))); ax.set_yticks(range(len(num_cols)))
    ax.set_xticklabels(num_cols, rotation=45, ha="right", color="#94a3b8", fontsize=8)
    ax.set_yticklabels(num_cols, color="#94a3b8", fontsize=8)
    plt.colorbar(im, ax=ax)
    ax.set_title("Correlation Heatmap", color="white")
    ax.set_facecolor("#0e1318"); fig.patch.set_facecolor("#080c10")
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "correlation_heatmap.png"), dpi=100, bbox_inches="tight")
    plt.close()
    charts.append({"type": "heatmap", "column": "all", "file": "correlation_heatmap.png"})

results["charts"] = charts

if len(num_cols) >= 2 and len(df) >= 20:
    target   = num_cols[-1]
    features = num_cols[:-1]
    X = df[features].values; y = df[target].values
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2, random_state=42)
    sc = StandardScaler()
    Xtr = sc.fit_transform(Xtr); Xte = sc.transform(Xte)
    model = RandomForestRegressor(n_estimators=100, random_state=42)
    model.fit(Xtr, ytr); ypred = model.predict(Xte)
    imps = sorted(
        [{"feature": f, "importance": round(float(i),4)} for f,i in zip(features, model.feature_importances_)],
        key=lambda x: x["importance"], reverse=True
    )
    results["ml"] = {
        "target": target, "features": features,
        "r2":   round(float(r2_score(yte,ypred)),4),
        "rmse": round(float(np.sqrt(mean_squared_error(yte,ypred))),4),
        "feature_importance": imps[:10],
    }
    fig, ax = plt.subplots(figsize=(8,5))
    top = imps[:8]
    ax.barh([x["feature"] for x in top],[x["importance"] for x in top],color="#00e5a0",edgecolor="#0a0f14")
    ax.set_title(f"Feature Importance - {target}", color="white")
    ax.tick_params(colors="#94a3b8")
    ax.set_facecolor("#0e1318"); fig.patch.set_facecolor("#080c10")
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir,"feature_importance.png"),dpi=100,bbox_inches="tight")
    plt.close()
    results["charts"].append({"type":"feature_importance","column":target,"file":"feature_importance.png"})

df.to_csv(os.path.join(output_dir,"cleaned_data.csv"),index=False)
with open(os.path.join(output_dir,"results.json"),"w") as f:
    json.dump(results, f, indent=2, default=str)
print("DONE")
'''

OCTAVE_SCRIPT = r'''% Auto-generated Octave analysis script — Research Cloud Portal
pkg load statistics io;
args = argv();
csv_path   = args{1};
output_dir = args{2};
mkdir(output_dir);

try
  data = csvread(csv_path, 1, 0);
catch e
  data = csvread(csv_path);
end

[rows, cols] = size(data);
s.rows = rows; s.cols = cols;
if rows > 0
  s.means = mean(data); s.stds = std(data);
  s.mins  = min(data);  s.maxs = max(data);
end

savejson('', s, fullfile(output_dir, 'results.json'));
csvwrite(fullfile(output_dir, 'cleaned_data.csv'), data);
printf('DONE\n');
'''

R_SCRIPT = r'''#!/usr/bin/env Rscript
# Auto-generated R analysis script — Research Cloud Portal
args <- commandArgs(trailingOnly=TRUE)
csv_path   <- args[1]
output_dir <- args[2]
dir.create(output_dir, showWarnings=FALSE, recursive=TRUE)

library(jsonlite)
df     <- read.csv(csv_path)
num_df <- df[, sapply(df, is.numeric), drop=FALSE]
result <- list(
  rows    = nrow(df), cols = ncol(df), columns = names(df),
  means   = if (ncol(num_df)>0) as.list(colMeans(num_df, na.rm=TRUE)) else list(),
  sds     = if (ncol(num_df)>0) as.list(apply(num_df, 2, sd, na.rm=TRUE)) else list()
)
writeLines(toJSON(result, auto_unbox=TRUE), file.path(output_dir, "results.json"))
write.csv(df, file.path(output_dir, "cleaned_data.csv"), row.names=FALSE)
cat("DONE\n")
'''

JULIA_SCRIPT = r'''#!/usr/bin/env julia
# Auto-generated Julia analysis script — Research Cloud Portal
using CSV, DataFrames, Statistics, JSON

csv_path   = ARGS[1]
output_dir = ARGS[2]
mkpath(output_dir)

df       = CSV.read(csv_path, DataFrame)
num_cols = names(df, Union{Int64,Float64})
result   = Dict{String,Any}("rows" => nrow(df), "cols" => ncol(df), "columns" => names(df))
if !isempty(num_cols)
  result["means"] = Dict(c => mean(skipmissing(df[!,c])) for c in num_cols)
  result["stds"]  = Dict(c => std(skipmissing(df[!,c]))  for c in num_cols)
end
open(joinpath(output_dir, "results.json"), "w") do f; JSON.print(f, result, 2); end
CSV.write(joinpath(output_dir, "cleaned_data.csv"), df)
println("DONE")
'''

# (script_content, run_cmd_template)  — {script} and {output} are filled at runtime
SCRIPTS: dict[str, tuple[str, str]] = {
    "python_ds": (PYTHON_DS_SCRIPT, "python3 {script} /tmp/dataset.csv {output}"),
    "octave":    (OCTAVE_SCRIPT,    "octave --no-gui {script} /tmp/dataset.csv {output}"),
    "r":         (R_SCRIPT,         "Rscript {script} /tmp/dataset.csv {output}"),
    "julia":     (JULIA_SCRIPT,     "julia {script} /tmp/dataset.csv {output}"),
}

EXT_MAP = {"python_ds": "py", "octave": "m", "r": "R", "julia": "jl"}


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def _update_db(flask_app, job_id: str, status: str, **kw) -> None:
    with flask_app.app_context():
        from app.extensions import db
        from app.models import Job
        job = Job.query.filter_by(job_id=job_id).first()
        if job is None:
            return
        job.status     = status
        job.updated_at = time.time()
        for k, v in kw.items():
            setattr(job, k, v)
        db.session.commit()


# ---------------------------------------------------------------------------
# OpenNebula helpers
# ---------------------------------------------------------------------------

def _one() -> pyone.OneServer:
    return pyone.OneServer(ONE_ENDPOINT, session=f"{ONE_USER}:{ONE_PASS}")


def _provision_vm(cpu: int, ram_gb: int, gpu: bool) -> int:
    one    = _one()
    mem_mb = ram_gb * 1024
    extra  = f'CPU="{cpu}" VCPU="{cpu}" MEMORY="{mem_mb}"'
    if gpu:
        extra += ' PCI=[CLASS="0302",SHORT_ADDRESS="*",VENDOR="10de"]'
    vm_id: int = one.template.instantiate(TEMPLATE_ID, "", False, extra)
    logger.info("Provisioned VM id=%d", vm_id)
    return vm_id


def _resize_disk(vm_id: int) -> None:
    _one().vm.diskresize(vm_id, 0, str(DISK_SIZE_MB))
    logger.info("Disk resized vm=%d to %d MB", vm_id, DISK_SIZE_MB)


def _wait_for_running(vm_id: int) -> str:
    deadline = time.monotonic() + VM_BOOT_TIMEOUT
    one      = _one()
    while time.monotonic() < deadline:
        vm = one.vm.info(vm_id)
        if vm.STATE == 3 and vm.LCM_STATE == 3:
            nics = vm.TEMPLATE.get("NIC", {})
            ip   = (nics[0] if isinstance(nics, list) else nics).get("IP", "")
            if ip:
                return ip
        if vm.STATE in (7, 8):
            raise RuntimeError(f"VM {vm_id} entered failed state (state={vm.STATE})")
        time.sleep(VM_BOOT_INTERVAL)
    raise TimeoutError(f"VM {vm_id} did not reach RUNNING in {VM_BOOT_TIMEOUT}s")


def _terminate_vm(vm_id: int) -> None:
    try:
        _one().vm.action("terminate-hard", vm_id)
        logger.info("VM %d terminated", vm_id)
    except Exception as exc:
        logger.error("Could not terminate VM %d: %s", vm_id, exc)


# ---------------------------------------------------------------------------
# SSH helpers (Paramiko — no subprocess)
# ---------------------------------------------------------------------------

def _wait_for_ssh(ip: str) -> None:
    deadline = time.monotonic() + SSH_READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((ip, 22), timeout=5):
                return
        except OSError:
            time.sleep(SSH_READY_INTERVAL)
    raise TimeoutError(f"SSH not reachable at {ip} after {SSH_READY_TIMEOUT}s")


def _connect(ip: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=ip, username=SSH_USER,
                   key_filename=SSH_KEY_PATH, timeout=30, banner_timeout=60)
    return client


def _run(client: paramiko.SSHClient, cmd: str, timeout: int = 300) -> str:
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    rc  = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if rc != 0:
        raise RuntimeError(
            f"Command failed (rc={rc}):\n  {cmd}\n  STDERR: {err}\n  STDOUT: {out}"
        )
    return out


def _put(client: paramiko.SSHClient, local: str, remote: str) -> None:
    with client.open_sftp() as sftp:
        sftp.put(local, remote)


def _put_text(client: paramiko.SSHClient, content: str, remote: str) -> None:
    with client.open_sftp() as sftp:
        with sftp.open(remote, "w") as fh:
            fh.write(content)


def _get(client: paramiko.SSHClient, remote: str, local: str) -> bool:
    try:
        with client.open_sftp() as sftp:
            sftp.get(remote, local)
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def _bootstrap(client: paramiko.SSHClient, task_type: str) -> None:
    # Add internet route and DNS — may already exist; suppress error
    _run(client, f"sudo ip route add default via {GATEWAY} 2>&1 || true")
    _run(client, "echo 'nameserver 8.8.8.8' | sudo tee /etc/resolv.conf > /dev/null")
    _run(client, "echo 'nameserver 8.8.4.4' | sudo tee -a /etc/resolv.conf > /dev/null")

    pkgs = BOOTSTRAP_PKGS.get(task_type, [])
    if pkgs:
        _run(
            client,
            "sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
            f"sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {' '.join(pkgs)}",
            timeout=600,
        )


# ---------------------------------------------------------------------------
# Analysis dispatch
# ---------------------------------------------------------------------------

def _run_analysis(
    client: paramiko.SSHClient,
    task_type: str,
    job_id: str,
    local_result_dir: Path,
) -> dict:
    if task_type not in SCRIPTS:
        raise ValueError(f"Unknown task_type: {task_type!r}")

    content, cmd_tmpl = SCRIPTS[task_type]
    remote_script     = f"/tmp/analysis_{job_id}.{EXT_MAP[task_type]}"
    remote_output     = f"/tmp/output_{job_id}"

    _run(client, f"mkdir -p {remote_output}")
    _put_text(client, content, remote_script)

    cmd = cmd_tmpl.format(script=remote_script, output=remote_output)
    out = _run(client, cmd, timeout=600)
    logger.info("Analysis stdout: %s", out[:300])

    # Fetch all produced files back to portal
    local_result_dir.mkdir(parents=True, exist_ok=True)
    _, ls_out, _ = client.exec_command(f"ls {remote_output}/")
    ls_out.channel.recv_exit_status()
    for fname in ls_out.read().decode().split():
        fname = fname.strip()
        if fname:
            _get(client, f"{remote_output}/{fname}", str(local_result_dir / fname))

    results_json = local_result_dir / "results.json"
    if results_json.exists():
        with open(results_json) as fh:
            return json.load(fh)
    return {}


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------

@celery.task(
    bind=True,
    name="tasks_vm_unified.run_unified_pipeline",
    max_retries=0,
    track_started=True,
)
def run_unified_pipeline(
    self,
    job_id: str,
    dataset_path: str,
    task_type: str,
    cpu: int      = 2,
    ram_gb: int   = 4,
    gpu: bool     = False,
    keep_vm: bool = False,
) -> dict:
    """
    Unified VM pipeline Celery task.

    Parameters
    ----------
    job_id       : Job.job_id string (e.g. "A3F7C2B1")
    dataset_path : Absolute path to the uploaded CSV on the portal host
    task_type    : "python_ds" | "octave" | "r" | "julia"
    cpu          : vCPU count
    ram_gb       : RAM in GiB
    gpu          : Whether to request GPU passthrough
    keep_vm      : If True, do NOT terminate the VM after the job
    """
    from app import create_app
    flask_app = create_app()

    vm_int_id: int | None         = None
    ssh: paramiko.SSHClient | None = None
    result_data: dict              = {}

    def step(status: str, **kw) -> None:
        self.update_state(state="PROGRESS", meta={"step": status})
        _update_db(flask_app, job_id, status, **kw)

    try:
        step("provisioning")
        vm_int_id = _provision_vm(cpu=cpu, ram_gb=ram_gb, gpu=gpu)

        step("booting", vm_id=str(vm_int_id))
        vm_ip = _wait_for_running(vm_int_id)
        step("resizing_disk", vm_id=str(vm_int_id))
        _resize_disk(vm_int_id)
        step("booting", vm_id=str(vm_int_id))
        vm_ip = _wait_for_running(vm_int_id)
        step("waiting_ssh", vm_id=str(vm_int_id), vm_ip=vm_ip)
        _wait_for_ssh(vm_ip)
        ssh = _connect(vm_ip)
        step("bootstrapping")
        _bootstrap(ssh, task_type)
        step("uploading")
        _put(ssh, dataset_path, "/tmp/dataset.csv")

        step("running")
        local_dir   = RESULTS_DIR / job_id
        result_data = _run_analysis(ssh, task_type, job_id, local_dir)

        _update_db(
            flask_app, job_id, "completed",
            result_path=str(local_dir),
            result_file=f"{job_id}/results.json",
        )

    except Exception as exc:
        _update_db(flask_app, job_id, "failed", error_msg=str(exc))
        logger.error("Pipeline failed (job=%s):\n%s", job_id, traceback.format_exc())
        raise

    finally:
        if ssh:
            try: ssh.close()
            except Exception: pass
        if vm_int_id is not None and not keep_vm:
            _terminate_vm(vm_int_id)

    return {"status": "completed", "job_id": job_id,
            "vm_id": vm_int_id, "summary": result_data}
