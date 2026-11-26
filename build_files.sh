#!/bin/bash
set -euo pipefail
python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.8.0 torchvision==0.23.0
python -m pip install -r requirements.txt
export EASYOCR_MODULE_PATH="${EASYOCR_MODULE_PATH:-.ocr-cache}"
python -c "from easyocr import Reader; Reader(['en'], gpu=False)"
python manage.py collectstatic --noinput
