# --- STAGE 1: BUILDER ---
FROM eclipse-temurin:21-jdk AS builder
WORKDIR /app

# 1. Install tools
RUN apt-get update && apt-get install -y wget unzip git && rm -rf /var/lib/apt/lists/*

# 2. Download and extract
RUN wget -q -O audiveris.zip "https://www.dropbox.com/scl/fi/ehql5rgigwea1q7cwymsr/audiveris_source.zip?rlkey=m5rol41patcos7u2fxsp2mttb&st=3h8bdw36&dl=1" && \
    unzip -q audiveris.zip -d /app/temp_source && \
    # This line moves the actual content to /app, regardless of how many subfolders are in the ZIP
    find /app/temp_source -maxdepth 4 -name "gradlew" -execdir cp -rp . /app/ \; && \
    rm -rf audiveris.zip /app/temp_source

# 3. Build
RUN git init && \
    git config user.email "build@example.com" && \
    git config user.name "Builder" && \
    git add . && \
    git commit -m "initial" && \
    chmod +x gradlew && \
    ./gradlew clean installDist -x test --no-daemon -Dorg.gradle.jvmargs="-Xmx4g" && \
    # --- THE FIX: Find the output and move it to a stable path ---
    FINAL_PATH=$(find . -name Audiveris -type d | grep "build/install/Audiveris" | head -n 1) && \
    mkdir -p /app/final_app && \
    cp -rp "$FINAL_PATH"/. /app/final_app/

# --- STAGE 2: RUNNER ---
FROM eclipse-temurin:21-jre

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# --- THE FIX: Copy from the stable path we created ---
COPY --from=builder /app/final_app /app/Audiveris

# Copy your Python files and install requirements
COPY . .
RUN pip install --no-cache-dir -r requirements.txt

# Create necessary directories and set permissions
RUN mkdir -p /app/audiveris_home/.config/AudiverisLtd/audiveris && \
    chmod -R 777 /app/Audiveris /app/audiveris_home

EXPOSE 8080
CMD ["python", "app.py"]
