#!/usr/bin/env bash
set -e
cd /home/pi/Money_Tracker
git pull
sudo systemctl restart moneytracker.service
echo "----updated-----"
