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
WORKDIR /app
RUN GRADLE_PATH=$(find /app/audiveris_source -name gradlew | head -n 1) && \
    GRADLE_DIR=$(dirname "$GRADLE_PATH") && \
    cd "$GRADLE_DIR" && \
    chmod +x gradlew && \
    ./gradlew clean installDist -x test --no-daemon --parallel && \
    # Locate and move the final build
    REAL_BASE=$(ls -d build/install/Audiveris* | head -n 1) && \
    mkdir -p /app/Audiveris /app/audiveris_home/.config/AudiverisLtd/audiveris && \
    cp -rp "$REAL_BASE"/. /app/Audiveris/ && \
    # AGGRESSIVE CLEANUP: Delete source and gradle files to free space
    cd /app && \
    rm -rf /app/audiveris_source && \
    rm -rf /root/.gradle && \
    chmod -R 777 /app/Audiveris /app/audiveris_home
    
# 4. Set up your Python app as usual
WORKDIR /app
COPY . .
RUN pip3 install --break-system-packages -r requirements.txt

# 5. Env Vars we fixed earlier
ENV HOME=/app/audiveris_home
ENV JAVA_OPTS="-Djava.io.tmpdir=/app/tmp"
ENV LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/usr/local/lib
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/tessdata

RUN mkdir -p /app/audiveris_home /app/tmp uploads output

EXPOSE 5000
CMD ["python3", "app.py"]
