# Research Cloud Portal

A private cloud platform for researchers to launch on-demand virtual machines and run data analysis jobs — built from scratch.

## What it does

Users upload a dataset and select a software stack (Python, MATLAB/Octave, R, or Julia).  
The system handles everything automatically:

1. Provisions a real KVM virtual machine via OpenNebula
2. Resizes the VM disk to 10GB
3. Waits for boot and SSH availability
4. Installs required libraries
5. Uploads the dataset, runs the analysis
6. Fetches results and terminates the VM

Progress is tracked in real time through a step-by-step pipeline UI.

## Architecture

Browser → Flask → Celery Worker → OpenNebula API → KVM/QEMU VM
↓                         ↓
Redis                  SSH (Paramiko)
↓
SQLite (SQLAlchemy)


## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | Flask (Python) |
| Task queue | Celery + Redis |
| VM management | OpenNebula + PyOne |
| Hypervisor | KVM/QEMU |
| SSH automation | Paramiko |
| Database | SQLite + SQLAlchemy |
| Frontend | Jinja2 + Bootstrap |

## Features

- Role-based access: Admin and Researcher accounts
- Real-time pipeline: Provision → Boot → SSH → Bootstrap → Run → Results
- Admin dashboard: user management, audit logs, VM usage stats
- Supports Python/Data Science, MATLAB/Octave, R, and Julia workloads
- Automatic VM lifecycle management

## Setup

**Requirements:** Linux (Debian/Ubuntu), OpenNebula, Redis, Python 3.11+

```bash
cd rcp_v3
python3 -m venv ../venv && source ../venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in SECRET_KEY, ONE_USER, ONE_PASS

# Terminal 1 — Web server
python run.py

# Terminal 2 — Task worker
PYTHONPATH=$(pwd) celery -A tasks worker \
  --include=tasks_octave,tasks_vm,tasks_vm_unified \
  --loglevel=info
```

Open `http://127.0.0.1:5000/login`

## Author

**Mahmoud Ali** — CS Student at E-JUST  
[GitHub](https://github.com/Mahmoud-910) · [LinkedIn](https://www.linkedin.com/in/mahmoud-ali-7b98542ba)
