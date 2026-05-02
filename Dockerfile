# --- STAGE 1: BUILDER ---
FROM eclipse-temurin:21-jdk AS builder
WORKDIR /app

# 1. Install tools
RUN apt-get update && apt-get install -y wget unzip git && rm -rf /var/lib/apt/lists/*

# 2. Download and extract
RUN wget -q -O audiveris.zip "https://www.dropbox.com/scl/fi/ehql5rgigwea1q7cwymsr/audiveris_source.zip?rlkey=m5rol41patcos7u2fxsp2mttb&st=3h8bdw36&dl=1" && \
    unzip -q audiveris.zip -d /app/temp_source && \
    rm audiveris.zip

# 3. Build and Move (All in one logical block to keep paths consistent)
RUN GW_PATH=$(find /app/temp_source -name gradlew | head -n 1) && \
    cd $(dirname "$GW_PATH") && \
    # We are now in the root of the Audiveris source
    git init && \
    git config user.email "build@example.com" && \
    git config user.name "Builder" && \
    git add . && \
    git commit -m "initial" && \
    chmod +x gradlew && \
    # Run the build
    ./gradlew :app:installDist -x test --no-daemon -Dorg.gradle.jvmargs="-Xmx4g" && \
    # THE FIX: Move the result using the relative path we KNOW exists after a successful build
    mkdir -p /app/final_app && \
    mv app/build/install/Audiveris/* /app/final_app/ && \
    # Clean up the massive source folder immediately to save space
    rm -rf /app/temp_source

# --- STAGE 2: RUNNER ---
FROM eclipse-temurin:21-jre

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy the exact folder we secured in the builder stage
COPY --from=builder /app/final_app /app/Audiveris

# Copy your Python files and install requirements
COPY . .
RUN pip install --no-cache-dir -r requirements.txt

# Create necessary directories and set permissions
RUN mkdir -p /app/audiveris_home/.config/AudiverisLtd/audiveris && \
    chmod -R 777 /app/Audiveris /app/audiveris_home

EXPOSE 8080
CMD ["python", "app.py"]
