FROM python:3.11-slim

WORKDIR /app

# install dependencies
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot code
COPY . .

# expose a port for HTTP health (prevents sleeping)
EXPOSE 8080

CMD ["python", "bot.py"]
