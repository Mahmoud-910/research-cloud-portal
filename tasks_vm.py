"""
tasks_vm.py — Celery task that runs real processing on OpenNebula VMs via SSH.
Flow: upload CSV -> SCP to VM -> run Python script -> fetch results back
"""

import os
import subprocess
import json
import time
import uuid
import traceback
import tempfile
from celery_app import celery

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
RESULTS_FOLDER = os.path.join(BASE_DIR, "app", "static", "results")
UPLOAD_FOLDER  = os.path.join(BASE_DIR, "app", "static", "uploads")
SSH_KEY        = os.path.expanduser("~/.ssh/id_rsa")
SSH_USER       = "ubuntu"
SSH_OPTS       = "-o StrictHostKeyChecking=no -o ConnectTimeout=10"

os.makedirs(RESULTS_FOLDER, exist_ok=True)


def update_progress(task, step: str, percent: int):
    task.update_state(state="PROGRESS", meta={"step": step, "percent": percent})


def ssh_run(ip: str, command: str) -> tuple:
    """Run a command on the VM via SSH."""
    cmd = f"ssh {SSH_OPTS} -i {SSH_KEY} {SSH_USER}@{ip} '{command}'"
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def scp_to_vm(ip: str, local_path: str, remote_path: str) -> bool:
    """Copy a file to the VM."""
    cmd = f"scp {SSH_OPTS} -i {SSH_KEY} {local_path} {SSH_USER}@{ip}:{remote_path}"
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


def scp_from_vm(ip: str, remote_path: str, local_path: str) -> bool:
    """Fetch a file from the VM."""
    cmd = f"scp {SSH_OPTS} -i {SSH_KEY} {SSH_USER}@{ip}:{remote_path} {local_path}"
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


def get_vm_ip(vm_id: int) -> str:
    """Get VM IP from OpenNebula."""
    import pyone
    c = pyone.OneServer(
        os.environ.get("ONE_XMLRPC", "http://localhost:2633/RPC2"),
        session="{}:{}".format(
            os.environ.get("ONE_USER", "oneadmin"),
            os.environ.get("ONE_PASS", "oneadmin")
        )
    )
    vm = c.vm.info(vm_id)
    template = vm.TEMPLATE
    if isinstance(template, dict):
        ctx = template.get("CONTEXT", {})
        if isinstance(ctx, dict):
            return ctx.get("ETH0_IP", None)
        nic = template.get("NIC", {})
        if isinstance(nic, dict):
            return nic.get("IP", None)
    try:
        return vm.TEMPLATE.NIC.IP
    except Exception:
        pass
    try:
        return vm.TEMPLATE.CONTEXT.ETH0_IP
    except Exception:
        pass
    return None
ANALYSIS_SCRIPT = '''
import sys, json, os
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
operations = sys.argv[3].split(",")

os.makedirs(output_dir, exist_ok=True)
results = {}

# Read
df = pd.read_csv(csv_path)
results["overview"] = {
    "rows": int(df.shape[0]),
    "columns": int(df.shape[1]),
    "column_names": df.columns.tolist(),
    "memory_usage_kb": round(df.memory_usage(deep=True).sum() / 1024, 2),
}

# Clean
numeric_cols     = df.select_dtypes(include=[np.number]).columns.tolist()
categorical_cols = df.select_dtypes(include=["object"]).columns.tolist()
dupes            = int(df.duplicated().sum())
missing_before   = int(df.isnull().sum().sum())
df = df.drop_duplicates()
for col in numeric_cols:
    df[col] = df[col].fillna(df[col].median())
for col in categorical_cols:
    df[col] = df[col].fillna(df[col].mode()[0] if not df[col].mode().empty else "Unknown")
results["cleaning"] = {
    "duplicates_removed": dupes,
    "missing_before": missing_before,
    "missing_after": int(df.isnull().sum().sum()),
    "rows_after_clean": int(len(df)),
}

# Stats
if "stats" in operations and numeric_cols:
    desc = df[numeric_cols].describe().round(4)
    results["stats"] = {"descriptive": desc.to_dict()}
    if len(numeric_cols) > 1:
        corr   = df[numeric_cols].corr().round(4)
        pairs  = []
        for i, c1 in enumerate(numeric_cols):
            for c2 in numeric_cols[i+1:]:
                pairs.append({"col1": c1, "col2": c2, "correlation": round(float(corr.loc[c1,c2]),4)})
        pairs.sort(key=lambda x: abs(x["correlation"]), reverse=True)
        results["stats"]["top_correlations"] = pairs[:5]

# Charts
charts = []
if "charts" in operations:
    for col in numeric_cols[:4]:
        fig, ax = plt.subplots(figsize=(7,4))
        ax.hist(df[col].dropna(), bins=30, color="#00d4ff", edgecolor="#0a0f14", alpha=0.85)
        ax.set_title(f"Distribution of {col}", color="white")
        ax.set_facecolor("#0e1318"); fig.patch.set_facecolor("#080c10")
        ax.tick_params(colors="#94a3b8")
        for sp in ax.spines.values(): sp.set_edgecolor("#1a2230")
        plt.tight_layout()
        fname = f"hist_{col.replace(' ','_')}.png"
        plt.savefig(os.path.join(output_dir, fname), dpi=100, bbox_inches="tight")
        plt.close()
        charts.append({"type":"histogram","column":col,"file":fname})
    if len(numeric_cols) > 1:
        corr_data = df[numeric_cols].corr()
        fig, ax   = plt.subplots(figsize=(8,6))
        im = ax.imshow(corr_data.values, cmap="coolwarm", vmin=-1, vmax=1)
        ax.set_xticks(range(len(corr_data.columns)))
        ax.set_yticks(range(len(corr_data.columns)))
        ax.set_xticklabels(corr_data.columns, rotation=45, ha="right", color="#94a3b8", fontsize=9)
        ax.set_yticklabels(corr_data.columns, color="#94a3b8", fontsize=9)
        plt.colorbar(im, ax=ax)
        ax.set_title("Correlation Heatmap", color="white")
        ax.set_facecolor("#0e1318"); fig.patch.set_facecolor("#080c10")
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, "correlation_heatmap.png"), dpi=100, bbox_inches="tight")
        plt.close()
        charts.append({"type":"heatmap","column":"all","file":"correlation_heatmap.png"})
results["charts"] = charts

# ML
if "ml" in operations and len(numeric_cols) >= 2:
    target   = numeric_cols[-1]
    features = numeric_cols[:-1]
    X = df[features].values
    y = df[target].values
    if len(X) >= 20:
        X_train,X_test,y_train,y_test = train_test_split(X,y,test_size=0.2,random_state=42)
        sc = StandardScaler()
        X_train = sc.fit_transform(X_train)
        X_test  = sc.transform(X_test)
        model = RandomForestRegressor(n_estimators=100, random_state=42)
        model.fit(X_train, y_train)
        y_pred = model.predict(X_test)
        imps = [{"feature":f,"importance":round(float(i),4)} for f,i in zip(features,model.feature_importances_)]
        imps.sort(key=lambda x: x["importance"], reverse=True)
        results["ml"] = {
            "target_column": target,
            "feature_columns": features,
            "model": "RandomForestRegressor",
            "r2_score": round(float(r2_score(y_test,y_pred)),4),
            "rmse": round(float(np.sqrt(mean_squared_error(y_test,y_pred))),4),
            "train_samples": len(X_train),
            "test_samples": len(X_test),
            "feature_importance": imps[:10],
        }
        fig, ax = plt.subplots(figsize=(8,5))
        top = imps[:8]
        ax.barh([x["feature"] for x in top],[x["importance"] for x in top],color="#00e5a0",edgecolor="#0a0f14")
        ax.set_title(f"Feature Importance -> {target}", color="white")
        ax.tick_params(colors="#94a3b8")
        ax.set_facecolor("#0e1318"); fig.patch.set_facecolor("#080c10")
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir,"feature_importance.png"),dpi=100,bbox_inches="tight")
        plt.close()
        results["charts"].append({"type":"feature_importance","column":target,"file":"feature_importance.png"})

# Save
df.to_csv(os.path.join(output_dir,"cleaned_data.csv"),index=False)
results["cleaned_csv"] = "cleaned_data.csv"
with open(os.path.join(output_dir,"summary.json"),"w") as f:
    json.dump(results, f, indent=2, default=str)
print("DONE")
'''


@celery.task(bind=True, name="tasks_vm.run_on_vm")
def run_on_vm(self, job_id: str, vm_id: int, file_path: str, operations: list):
    """
    Runs real data processing on an OpenNebula VM via SSH.
    """
    result_folder = os.path.join(RESULTS_FOLDER, job_id)
    os.makedirs(result_folder, exist_ok=True)

    # Convert relative path to absolute
    if not os.path.isabs(file_path):
        file_path = os.path.join(BASE_DIR, file_path)

    try:
        update_progress(self, "Getting VM IP...", 5)
        ip = get_vm_ip(vm_id)
        if not ip:
            raise Exception(f"Could not get IP for VM {vm_id}")

        update_progress(self, f"Connecting to VM {ip}...", 10)

        # Wait for SSH to be ready
        for i in range(12):
            rc, _, _ = ssh_run(ip, "echo ready")
            if rc == 0:
                break
            time.sleep(5)
        else:
            raise Exception(f"VM {ip} not reachable via SSH")

        update_progress(self, "Uploading dataset to VM...", 20)

        remote_dir    = f"/tmp/job_{job_id}"
        remote_csv    = f"{remote_dir}/data.csv"
        remote_script = f"{remote_dir}/analyze.py"
        remote_output = f"{remote_dir}/output"

        ssh_run(ip, f"mkdir -p {remote_dir} {remote_output}")

        if not scp_to_vm(ip, file_path, remote_csv):
            raise Exception("Failed to upload dataset to VM")

        update_progress(self, "Uploading analysis script...", 30)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(ANALYSIS_SCRIPT)
            script_path = f.name

        if not scp_to_vm(ip, script_path, remote_script):
            raise Exception("Failed to upload script to VM")
        os.unlink(script_path)

        update_progress(self, "Running analysis on VM...", 40)

        ops_str = ",".join(operations)
        rc, stdout, stderr = ssh_run(
            ip,
            f"cd {remote_dir} && python3 {remote_script} {remote_csv} {remote_output} {ops_str}"
        )

        if rc != 0:
            raise Exception(f"Script failed on VM:\n{stderr}")

        update_progress(self, "Fetching results...", 80)

        # Fetch all result files
        rc2, file_list, _ = ssh_run(ip, f"ls {remote_output}/")
        fetched = []
        for fname in file_list.strip().split("\n"):
            fname = fname.strip()
            if fname:
                local_path = os.path.join(result_folder, fname)
                if scp_from_vm(ip, f"{remote_output}/{fname}", local_path):
                    fetched.append(fname)

        update_progress(self, "Cleaning up VM...", 95)
        ssh_run(ip, f"rm -rf {remote_dir}")

        # Fix chart file paths in summary
        summary_path = os.path.join(result_folder, "summary.json")
        if os.path.exists(summary_path):
            with open(summary_path) as f:
                summary = json.load(f)
            if "charts" in summary:
                for chart in summary["charts"]:
                    chart["file"] = f"{job_id}/{chart['file']}"
            if "cleaned_csv" in summary:
                summary["cleaned_csv"] = f"{job_id}/cleaned_data.csv"
            with open(summary_path, "w") as f:
                json.dump(summary, f, indent=2, default=str)

        update_progress(self, "Done!", 100)
        return {"status": "SUCCESS", "job_id": job_id, "vm_id": vm_id, "files": fetched}

    except Exception as e:
        error = traceback.format_exc()
        print(f"[ERROR] VM task failed: {error}")
        with open(os.path.join(result_folder, "error.txt"), "w") as f:
            f.write(error)
        raise self.retry(exc=e, max_retries=0)
