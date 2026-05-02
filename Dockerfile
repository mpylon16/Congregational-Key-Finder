# --- STAGE 1: BUILDER ---
FROM eclipse-temurin:21-jdk AS builder
WORKDIR /app
COPY --from=0 /app/audiveris_source /app/audiveris_source

RUN GRADLE_PATH=$(find /app/audiveris_source -name gradlew | head -n 1) && \
    GRADLE_DIR=$(dirname "$GRADLE_PATH") && \
    cd "$GRADLE_DIR" && \
    chmod +x gradlew && \
    ./gradlew clean installDist -x test -q --no-daemon

# --- STAGE 2: RUNNER ---
# Use the official Java 21 runtime as the base
FROM eclipse-temurin:21-jre

# Install Python and Tesseract from standard repositories (no custom keys needed)
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    tesseract-ocr \
    wget \
    unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create and activate a Python virtual environment
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy the built Audiveris from the builder stage
COPY --from=builder /app/audiveris_source/app/build/install/Audiveris /app/Audiveris

# Copy your Python app files
COPY . .

# Install dependencies inside the virtual environment
RUN pip install --no-cache-dir -r requirements.txt

# Set permissions
RUN mkdir -p /app/audiveris_home/.config/AudiverisLtd/audiveris && \
    chmod -R 777 /app/Audiveris /app/audiveris_home

EXPOSE 8080
CMD ["python", "app.py"]
