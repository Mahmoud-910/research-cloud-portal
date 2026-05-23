# Research Cloud Portal v3 — Complete Setup Guide

## User Accounts

| Full Name    | Username     | Password        | Role       |
|-------------|--------------|-----------------|------------|
| Mahmoud Ali | mahmoud.ali  | Admin@RCP2025!  | Admin      |
| User One    | user1        | User1@RCP2025!  | Researcher |
| User Two    | user2        | User2@RCP2025!  | Researcher |

> All users are prompted to change their password on first login.

---

## Prerequisites

| Software    | Version   | Purpose                        |
|-------------|-----------|-------------------------------|
| Python      | 3.10+     | Backend runtime                |
| Redis       | 6+        | Celery broker + result backend |
| OpenNebula  | 6.x       | VM provisioning (optional)     |
| GNU Octave  | 6+        | MATLAB script runner (optional)|

---

## Quick Start — Local Development

### Step 1 — Clone / extract the project

```bash
cd ~
# If using this folder directly:
cd research_cloud_portal_v3
```

### Step 2 — Create virtual environment

```bash
python3 -m venv venv
source venv/bin/activate          # Linux / macOS
# venv\Scripts\activate           # Windows
```

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 4 — Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```env
SECRET_KEY=<run: python3 -c "import secrets; print(secrets.token_hex(32))">
FLASK_ENV=development
ADMIN_PASSWORD=Admin@RCP2025!
USER1_PASSWORD=User1@RCP2025!
USER2_PASSWORD=User2@RCP2025!
ONE_XMLRPC=http://localhost:2633/RPC2
ONE_USER=oneadmin
ONE_PASS=your_oneadmin_password
```

### Step 5 — Start Redis

```bash
# Ubuntu / Debian
sudo systemctl start redis

# macOS
brew services start redis

# Docker (quickest)
docker run -d -p 6379:6379 --name redis redis:7-alpine
```

Verify Redis is running:
```bash
redis-cli ping   # should return PONG
```

### Step 6 — Initialise the database

```bash
flask db init       # only on first run (creates migrations/ folder)
flask db migrate -m "initial schema"
flask db upgrade
```

Or, if you want Flask to auto-create tables on startup (default):
```bash
# Skip the flask db commands — tables are created automatically by create_app()
```

### Step 7 — Start the Celery worker

Open a **second terminal**, activate the venv, then:

```bash
source venv/bin/activate
celery -A tasks worker \
  --include=tasks_octave,tasks_vm,tasks_vm_unified \
  --loglevel=info \
  --concurrency=4
```

Keep this terminal open. All background VM pipelines run here.

### Step 8 — Start Flask

In your **first terminal**:

```bash
python run.py
```

Open: **http://127.0.0.1:5000**

Log in with `mahmoud.ali` / `Admin@RCP2025!`

---

## Running Tests

```bash
source venv/bin/activate
pytest tests.py -v
```

Expected: **12 tests passed**

---

## Production Deployment

### Option A — Gunicorn + Nginx (recommended)

#### Step 1 — Install production server

```bash
pip install gunicorn
```

#### Step 2 — Start Gunicorn

```bash
gunicorn \
  --workers 4 \
  --bind 0.0.0.0:5000 \
  --timeout 120 \
  --access-logfile /var/log/rcp/access.log \
  --error-logfile  /var/log/rcp/error.log \
  "run:app"
```

#### Step 3 — Nginx reverse proxy

Install Nginx:
```bash
sudo apt install nginx -y
```

Create `/etc/nginx/sites-available/rcp`:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 16M;

    location /static/ {
        alias /home/ubuntu/research_cloud_portal_v3/app/static/;
        expires 7d;
    }

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
```

Enable the site:
```bash
sudo ln -s /etc/nginx/sites-available/rcp /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

#### Step 4 — HTTPS with Let's Encrypt

```bash
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d your-domain.com
```

Set in `.env`:
```env
SESSION_COOKIE_SECURE=True
FLASK_ENV=production
```

#### Step 5 — Systemd services

**Flask / Gunicorn** — `/etc/systemd/system/rcp-web.service`:

```ini
[Unit]
Description=Research Cloud Portal — Gunicorn
After=network.target redis.service

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/research_cloud_portal_v3
EnvironmentFile=/home/ubuntu/research_cloud_portal_v3/.env
ExecStart=/home/ubuntu/research_cloud_portal_v3/venv/bin/gunicorn \
          --workers 4 --bind 0.0.0.0:5000 --timeout 120 "run:app"
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**Celery worker** — `/etc/systemd/system/rcp-celery.service`:

```ini
[Unit]
Description=Research Cloud Portal — Celery Worker
After=network.target redis.service

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/research_cloud_portal_v3
EnvironmentFile=/home/ubuntu/research_cloud_portal_v3/.env
ExecStart=/home/ubuntu/research_cloud_portal_v3/venv/bin/celery \
          -A tasks worker \
          --include=tasks_octave,tasks_vm,tasks_vm_unified \
          --loglevel=info \
          --concurrency=4
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start both:
```bash
sudo systemctl daemon-reload
sudo systemctl enable  rcp-web rcp-celery
sudo systemctl start   rcp-web rcp-celery
sudo systemctl status  rcp-web rcp-celery
```

View logs:
```bash
sudo journalctl -u rcp-web    -f
sudo journalctl -u rcp-celery -f
```

---

### Option B — Docker Compose

Create `docker-compose.yml`:

```yaml
version: '3.9'

services:
  redis:
    image: redis:7-alpine
    restart: always
    ports: ["6379:6379"]

  web:
    build: .
    restart: always
    ports: ["5000:5000"]
    env_file: .env
    environment:
      - CELERY_BROKER_URL=redis://redis:6379/0
      - CELERY_RESULT_BACKEND=redis://redis:6379/0
    depends_on: [redis]
    volumes:
      - ./app/static/uploads:/app/app/static/uploads
      - ./app/static/results:/app/app/static/results
      - ./instance:/app/instance

  worker:
    build: .
    restart: always
    command: >
      celery -A tasks worker
      --include=tasks_octave,tasks_vm,tasks_vm_unified
      --loglevel=info --concurrency=4
    env_file: .env
    environment:
      - CELERY_BROKER_URL=redis://redis:6379/0
      - CELERY_RESULT_BACKEND=redis://redis:6379/0
    depends_on: [redis]
    volumes:
      - ./app/static/uploads:/app/app/static/uploads
      - ./app/static/results:/app/app/static/results
```

Create `Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p app/static/uploads app/static/results instance

CMD ["gunicorn", "--workers", "4", "--bind", "0.0.0.0:5000", "--timeout", "120", "run:app"]
```

Run:
```bash
docker compose up -d
docker compose logs -f
```

---

## OpenNebula Integration

The VM pipeline only works when OpenNebula is running.
Without it, launch jobs will fail immediately.

### Verify connection:

```bash
source venv/bin/activate
python3 -c "
import pyone, os
from dotenv import load_dotenv
load_dotenv()
c = pyone.OneServer(os.environ['ONE_XMLRPC'],
                    session=f\"{os.environ['ONE_USER']}:{os.environ['ONE_PASS']}\")
vms = c.vmpool.info(-1,-1,-1,3)
print(f'Connected. Running VMs: {len(vms.VM)}')
"
```

### Resume test VM:

```bash
onevm resume 56     # VM ID 56 is the tested Ubuntu 22.04 instance
```

### Required: SSH key setup

The portal host must have its SSH public key in Template #14's authorized_keys.
Verify with:

```bash
ssh -i ~/.ssh/id_rsa ubuntu@192.168.122.103 "echo SSH OK"
```

---

## File Structure

```
research_cloud_portal_v3/
├── app/
│   ├── __init__.py           ← App factory, seeds 3 users
│   ├── extensions.py         ← db, migrate, csrf
│   ├── models.py             ← User, Job, AuditLog
│   ├── security.py           ← Auth decorators, rate limit, audit helper
│   ├── auth/routes.py        ← Login, logout, change-password
│   ├── jobs/routes.py        ← Dashboard, /launch, /status, /results
│   ├── admin/routes.py       ← Users, audit logs, VM usage panels
│   ├── analysis/routes.py    ← Local pandas+ML pipeline
│   ├── octave/routes.py      ← GNU Octave script runner
│   ├── templates/            ← All Jinja2 HTML templates
│   └── static/
│       ├── css/portal.css    ← Full dark theme design system
│       ├── uploads/          ← Researcher CSV uploads
│       └── results/          ← Analysis output (charts, CSVs, JSON)
├── tasks.py                  ← Celery: local pandas+ML pipeline
├── tasks_octave.py           ← Celery: GNU Octave runner
├── tasks_vm.py               ← Celery: legacy VM SSH pipeline
├── tasks_vm_unified.py       ← Celery: unified end-to-end VM pipeline ← MAIN
├── celery_app.py             ← Celery + Redis config
├── run.py                    ← Entry point
├── tests.py                  ← 12 pytest tests
├── requirements.txt
├── .env.example              ← Copy to .env and fill in values
├── .gitignore
└── SETUP.md                  ← This file
```

---

## Quick Commands Reference

```bash
# Start everything (development)
sudo systemctl start redis
source venv/bin/activate
celery -A tasks worker --include=tasks_octave,tasks_vm,tasks_vm_unified --loglevel=info &
python run.py

# Run tests
pytest tests.py -v

# Check Celery workers
celery -A tasks inspect active

# Check Redis
redis-cli ping

# View systemd logs (production)
sudo journalctl -u rcp-web -f
sudo journalctl -u rcp-celery -f

# Database migration (after model changes)
flask db migrate -m "description"
flask db upgrade

# Reset database (CAUTION: destroys all data)
rm instance/portal.db
python run.py   # recreates tables + seeds users
```

---

## Security Notes

- Change all default passwords immediately after first login
- In production: set `SESSION_COOKIE_SECURE=True` and serve over HTTPS
- Never commit `.env` to version control (it is in `.gitignore`)
- Rotate `SECRET_KEY` after initial setup; all existing sessions will be invalidated
- The Admin account (mahmoud.ali) has access to all users, all jobs, and the full audit log
