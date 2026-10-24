FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dexi_x_radar ./dexi_x_radar
COPY PLAYBOOK.md watchlist.txt ./
COPY prompts ./prompts

# DB_PATH points at the Railway volume (/data); the image itself holds no state.
CMD ["python", "-m", "dexi_x_radar", "serve"]
