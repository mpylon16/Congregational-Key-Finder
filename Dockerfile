# --- STAGE 1: BUILDER ---
FROM eclipse-temurin:21-jdk AS builder
WORKDIR /app

# Install tools needed to download and extract
RUN apt-get update && apt-get install -y wget unzip && rm -rf /var/lib/apt/lists/*

# Download and extract the source
RUN wget -q -O audiveris.zip "https://www.dropbox.com/scl/fi/ehql5rgigwea1q7cwymsr/audiveris_source.zip?rlkey=m5rol41patcos7u2fxsp2mttb&st=3h8bdw36&dl=1" && \
    unzip -q audiveris.zip -d /app/audiveris_source && \
    rm audiveris.zip

# Build the application
RUN GRADLE_PATH=$(find /app/audiveris_source -name gradlew | head -n 1) && \
    GRADLE_DIR=$(dirname "$GRADLE_PATH") && \
    cd "$GRADLE_DIR" && \
    chmod +x gradlew && \
    ./gradlew clean installDist -x test -q --no-daemon

# --- STAGE 2: RUNNER ---
FROM eclipse-temurin:21-jre

# Install Python and Tesseract
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create and activate virtual environment
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy the built Audiveris from the builder stage
COPY --from=builder /app/audiveris_source/app/build/install/Audiveris /app/Audiveris

# Copy your Python app files
COPY . .

# Install dependencies inside the virtual environment
RUN pip install --no-cache-dir -r requirements.txt

# Create necessary directories and set permissions
RUN mkdir -p /app/audiveris_home/.config/AudiverisLtd/audiveris && \
    chmod -R 777 /app/Audiveris /app/audiveris_home

EXPOSE 8080
CMD ["python", "app.py"]
