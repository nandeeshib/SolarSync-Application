"""
ETL Scheduler — runs etl_pipeline.py every hour automatically.
Used when deploying as a long-running process on Render.com background worker.
"""
import schedule, time, logging, os
from etl_pipeline import run_pipeline

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

USER_ID = os.getenv("DEFAULT_USER_ID", None)

def job():
    log.info("Scheduled ETL run starting...")
    run_pipeline(user_id=USER_ID)

schedule.every(1).hours.do(job)
schedule.every().day.at("06:00").do(job)   # morning run
schedule.every().day.at("18:00").do(job)   # evening run

log.info("ETL Scheduler started — running every hour")
job()   # immediate first run

while True:
    schedule.run_pending()
    time.sleep(60)
