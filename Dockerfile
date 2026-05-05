# --- STAGE 1: BUILDER ---
FROM eclipse-temurin:21-jdk AS builder
WORKDIR /app

# 1. Install necessary tools (Git is required for Audiveris versioning task)
# Use cache mounts to keep unzip/git/wget in the build cache
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y wget unzip git && rm -rf /var/lib/apt/lists/*

# 2. Extract and Flatten (Eliminates the "nested folder in ZIP" issue forever)
RUN wget -q -O audiveris.zip "https://www.dropbox.com/scl/fi/ehql5rgigwea1q7cwymsr/audiveris_source.zip?rlkey=m5rol41patcos7u2fxsp2mttb&st=3h8bdw36&dl=1" && \
    unzip -q audiveris.zip -d /app/temp_source && \
    # Locate the directory containing gradlew and move its CONTENTS to /app
    GW_DIR=$(dirname $(find /app/temp_source -name gradlew | head -n 1)) && \
    cp -rp "$GW_DIR"/. /app/ && \
    rm -rf audiveris.zip /app/temp_source

# 3. Build (Git init required because the ZIP doesn't have a .git folder)
RUN git init && \
    git config user.email "build@example.com" && \
    git config user.name "Builder" && \
    git add . && \
    git commit -m "initial" && \
    chmod +x gradlew && \
    # Build the distribution (Memory limit prevents Runner crashes)
    ./gradlew :app:installDist -x test --no-daemon -Dorg.gradle.jvmargs="-Xmx4g" && \
    # 4. CAPTURE THE ARTIFACT
    # Look into the install directory and move the one folder found there to /app/final_app
    INSTALL_PARENT="/app/app/build/install" && \
    DIR_NAME=$(ls "$INSTALL_PARENT" | head -n 1) && \
    mv "$INSTALL_PARENT/$DIR_NAME" /app/final_app && \
    cp -r /app/res /app/final_app/res && \
    echo "Successfully captured artifact from $DIR_NAME"

# --- STAGE 2: RUNNER ---
FROM eclipse-temurin:21-jre

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Setup Python Virtual Env
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy the built Audiveris
COPY --from=builder /app/final_app /app/Audiveris

# Ensure permissions
RUN mkdir -p /app/audiveris_home && \
    chmod -R 777 /app/Audiveris /app/audiveris_home

# We can remove the 'sed' hack. We are going straight to the OS level now.
ENV TESSDATA_PREFIX="/usr/share/tesseract-ocr/5/tessdata"
    
# Copy Python app files
COPY . .
RUN pip install --no-cache-dir -r requirements.txt


# Set Environment Variables
# 1. Change the Linux HOME variable for the whole container
# 2. Point Java home to our writable directory
# 3. Point Tesseract to the system-installed data
ENV HOME="/app/audiveris_home"
ENV JAVA_OPTS="-Duser.home=/app/audiveris_home"
ENV TESSDATA_PREFIX="/usr/share/tesseract-ocr/5/tessdata"

EXPOSE 8080
CMD ["python", "app.py"]
