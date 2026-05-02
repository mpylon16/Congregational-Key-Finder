FROM eclipse-temurin:21-jdk

# 1. Install EVERYTHING + Git (Crucial for the Audiveris version script)
RUN apt-get update && apt-get install -y \
    python3 python3-pip tesseract-ocr tesseract-ocr-eng \
    libtesseract-dev libleptonica-dev libgomp1 poppler-utils \
    fontconfig libfreetype6 libxext6 libxrender1 libxtst6 \
    wget unzip git \
    && apt-get clean

WORKDIR /app

# 2. DOWNLOAD & UNZIP (Optimized)
RUN wget -q -O audiveris.zip "https://www.dropbox.com/scl/fi/ehql5rgigwea1q7cwymsr/audiveris_source.zip?rlkey=m5rol41patcos7u2fxsp2mttb&st=fi6sjjdc&dl=1" && \
    unzip -q audiveris.zip -d /app/audiveris_source && \
    rm audiveris.zip

# 3. BUILD Audiveris & CLEANUP
# --- STAGE 1: BUILDER ---
FROM openjdk:21-jdk-slim AS builder
WORKDIR /app
COPY --from=0 /app/audiveris_source /app/audiveris_source

RUN GRADLE_PATH=$(find /app/audiveris_source -name gradlew | head -n 1) && \
    GRADLE_DIR=$(dirname "$GRADLE_PATH") && \
    cd "$GRADLE_DIR" && \
    chmod +x gradlew && \
    ./gradlew clean installDist -x test -q --no-daemon

# --- STAGE 2: RUNNER (Final Image) ---
FROM python:3.11-slim
# Install Java and Tesseract in the final slim image
RUN apt-get update && apt-get install -y \
    openjdk-17-jre-headless \
    tesseract-ocr \
    wget \
    unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Copy the built Audiveris from the builder stage
COPY --from=builder /app/audiveris_source/app/build/install/Audiveris /app/Audiveris
# Copy your Python app files
COPY . .
RUN pip install --no-cache-dir -r requirements.txt

# Set permissions
RUN mkdir -p /app/audiveris_home/.config/AudiverisLtd/audiveris && \
    chmod -R 777 /app/Audiveris /app/audiveris_home

EXPOSE 8080
CMD ["python", "app.py"]
