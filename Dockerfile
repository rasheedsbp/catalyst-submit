FROM python:3.12-slim

# LibreOffice powers the PDF export and the page-accurate preview; poppler
# rasterises those pages. Without them the app runs but loses both features.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        poppler-utils \
        fonts-liberation \
        fonts-dejavu-core \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p data && useradd -m -u 1000 appuser && chown -R appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_MAX_UPLOAD_SIZE=50

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
