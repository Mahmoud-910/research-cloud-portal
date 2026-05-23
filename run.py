"""
run.py — Entry point for Research Cloud Portal v3.
Development:  python run.py
Production:   gunicorn -w 4 -b 0.0.0.0:5000 "run:app"
"""
import os
from dotenv import load_dotenv
load_dotenv()

from app import create_app

app = create_app()

if __name__ == "__main__":
    print("=" * 60)
    print("  Research Cloud Portal v3  —  Production Build")
    print("  Users:   mahmoud.ali (Admin) | user1 | user2")
    print("  DB:     ", os.environ.get("DATABASE_URL", "sqlite (instance/portal.db)"))
    print("  Broker: ", os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"))
    print("=" * 60)
    app.run(
        debug=os.environ.get("FLASK_ENV") == "development",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
    )
