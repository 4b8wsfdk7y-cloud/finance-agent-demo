FROM python:3.12-slim

# 系统依赖:OCR + PDF 转图片 + 中文语言包
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-eng \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

ENV PATH="/opt/venv/bin:$PATH"
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

COPY . .

EXPOSE 5000
CMD ["python", "app.py"]
