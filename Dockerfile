FROM python:3.12-slim-bookworm

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt \
    && python -c "from rembg import new_session; new_session('isnet-general-use')"

COPY app.py .
ENV WEB=1
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-u", "app.py"]
