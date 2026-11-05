FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=ICT-7

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY seismic_mcp_server.py earthquakes.py tools.json ./
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/healthz', timeout=2).close()"]
CMD ["python", "-m", "uvicorn", "seismic_mcp_server:app", "--host", "0.0.0.0", "--port", "8000"]
