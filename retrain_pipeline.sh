#!/bin/bash
set -e
cd /Users/mattiacalastri/btc_predictions
LOG="/tmp/btcbot_retrain_$(date +%Y%m%d).log"
echo "[$(date)] Starting XGBoost retrain pipeline" >> $LOG
/usr/bin/python3 build_dataset.py >> $LOG 2>&1
/usr/bin/python3 train_xgboost.py >> $LOG 2>&1
# Reload calibration on Railway
curl -s -X POST https://web-production-e27d0.up.railway.app/reload-calibration \
  -H "X-API-Key: REDACTED_BOT_API_KEY" \
  >> $LOG 2>&1
echo "[$(date)] Retrain pipeline completed" >> $LOG
