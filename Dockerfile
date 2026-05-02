# --- STAGE 1: BUILDER ---
FROM eclipse-temurin:21-jdk AS builder
WORKDIR /app

# Install everything needed in one layer
RUN apt-get update && \
    apt-get install -y wget unzip git && \
    rm -rf /var/lib/apt/lists/* && \
    # Diagnostic: Prove git is installed
    git --version

# Download and extract
RUN wget -q -O audiveris.zip "https://www.dropbox.com/scl/fi/ehql5rgigwea1q7cwymsr/audiveris_source.zip?rlkey=m5rol41patcos7u2fxsp2mttb&st=3h8bdw36&dl=1" && \
    # Unzip directly into /app to avoid nested 'audiveris_source/audiveris_source' folders
    unzip -q audiveris.zip -d /app/temp_source && \
    mv /app/temp_source/*/* /app/ 2>/dev/null || mv /app/temp_source/* /app/ && \
    rm -rf audiveris.zip /app/temp_source

# Build
RUN git init && \
    git config user.email "build@example.com" && \
    git config user.name "Builder" && \
    git add . && \
    git commit -m "initial" && \
    chmod +x gradlew && \
    # Apply memory limit and stacktrace for final safety
    ./gradlew clean installDist -x test --no-daemon --stacktrace -Dorg.gradle.jvmargs="-Xmx4g"

# --- STAGE 2: RUNNER ---
FROM eclipse-temurin:21-jre

# Standard dependencies
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Setup Python Virtual Env
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy the build output (the path is now predictable because we flattened it)
COPY --from=builder /app/app/build/install/Audiveris /app/Audiveris

# Copy your Python files
COPY . .

# Install requirements
RUN pip install --no-cache-dir -r requirements.txt

# Final permissions
RUN mkdir -p /app/audiveris_home/.config/AudiverisLtd/audiveris && \
    chmod -R 777 /app/Audiveris /app/audiveris_home

EXPOSE 8080
CMD ["python", "app.py"]
