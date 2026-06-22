#!/bin/bash
# Script untuk menjalankan bot di Linux (VPS)

# Pastikan berada di direktori bot
cd "$(dirname "$0")"

# Jalankan bot
echo "Menjalankan bot..."
python3 boss_time_v2.py
