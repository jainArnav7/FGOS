FROM python:3.12-slim

# Install system dependencies required for audio processing (edge-tts / ffmpeg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency definitions and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your bot code files into the container
COPY . .

# Run your main script
CMD ["python", "bot.py"]
