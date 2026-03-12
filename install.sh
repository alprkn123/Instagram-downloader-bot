#!/bin/bash
cd /home/container

echo "🚀 Installing Node.js dependencies..."
npm install

echo "📦 Installing Python for Vosk..."
apt-get update && apt-get install -y python3 python3-pip ffmpeg wget unzip

echo "📥 Downloading Vosk model..."
mkdir -p models
cd models
wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip vosk-model-small-en-us-0.15.zip
rm vosk-model-small-en-us-0.15.zip
cd ..

echo "📦 Installing Python packages..."
pip3 install vosk

echo "✅ Installation complete!"
