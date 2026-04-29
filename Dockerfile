FROM eclipse-temurin:21-jdk

# 1. Install EVERYTHING + Git (Crucial for the Audiveris version script)
RUN apt-get update && apt-get install -y \
    python3 python3-pip tesseract-ocr tesseract-ocr-eng \
    libtesseract-dev libleptonica-dev libgomp1 poppler-utils \
    fontconfig libfreetype6 libxext6 libxrender1 libxtst6 \
    wget unzip git \
    && apt-get clean

WORKDIR /app

# 2. DOWNLOAD & CLEAN
RUN wget -O audiveris_source.zip "https://www.dropbox.com/scl/fi/ehql5rgigwea1q7cwymsr/audiveris_source.zip?rlkey=m5rol41patcos7u2fxsp2mttb&st=fi6sjjdc&dl=1" && \
    unzip audiveris_source.zip && \
    rm audiveris_source.zip

# We use a separate RUN for the cleanup to keep it clean
RUN find . -name ".gradle" -type d -exec rm -rf {} + && \
    find . -name "build" -type d -exec rm -rf {} +

# 3. BUILD Audiveris
WORKDIR /app
RUN GRADLE_PATH=$(find . -name gradlew | head -n 1) && \
    cd $(dirname "$GRADLE_PATH") && \
    # We remove the local caches again just to be safe
    rm -rf .gradle .idea build out bin && \
    chmod +x gradlew && \
    # Now that git is installed, this task won't fail!
    ./gradlew clean build -x test --no-daemon
    
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
